//! Isolated laboratory prototype, not a template runtime or production code.
use std::collections::VecDeque;
use std::future::{Future, poll_fn};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::pin::Pin;
use std::task::{Context, Poll, Waker};
use pyo3::coroutine::CancelHandle;
use pyo3::exceptions::{PyAttributeError, PyNameError, PyRuntimeError, PyStopAsyncIteration, PyStopIteration, PyTypeError};
use pyo3::prelude::*;
use pyo3::ffi;
use pyo3::types::{PyDict, PyModule};

#[pyclass]
struct WakeCallback { wake: Waker }
#[pymethods]
impl WakeCallback {
    fn __call__(&self, _future: &Bound<'_, PyAny>) { self.wake.wake_by_ref(); }
}

// In Python 3.11 task.__repr__ inspects the coroutine. Defer that inspection
// until after PyO3 has released its mutable borrow of the running coroutine.
#[pyclass]
struct FormatErrorCallback {
    task: Py<PyAny>, value: Py<PyAny>, mode: u8,
    result: Arc<Mutex<Option<PyResult<Py<PyAny>>>>>, wake: Waker,
}
#[pymethods]
impl FormatErrorCallback {
    fn __call__(&self, py: Python<'_>) {
        let result = (|| -> PyResult<Py<PyAny>> {
            let task = self.task.bind(py).repr()?.extract::<String>()?;
            let message = match self.mode {
                0 => format!("Task {task} got Future {} attached to a different loop", self.value.bind(py).repr()?),
                1 => format!("yield was used instead of yield from in task {task} with {}", self.value.bind(py).repr()?),
                2 => format!("Task cannot await on itself: {task}"),
                _ => format!("yield was used instead of yield from for generator in task {task} with {}", self.value.bind(py).repr()?),
            };
            Ok(PyRuntimeError::new_err(message).into_value(py).into_any())
        })();
        *self.result.lock().unwrap() = Some(result);
        self.wake.wake_by_ref();
    }
}

struct DirectAwait<'a> {
    iterator: Py<PyAny>, waiting: Option<Py<PyAny>>, thrown: Option<Py<PyAny>>,
    formatting: Option<Arc<Mutex<Option<PyResult<Py<PyAny>>>>>>,
    cancel: &'a mut CancelHandle,
}
impl Future for DirectAwait<'_> {
    type Output = PyResult<Py<PyAny>>;
    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        Python::attach(|py| {
            let mut thrown = match self.cancel.poll_cancelled(cx) {
                Poll::Ready(error) => {
                    // asyncio cancellation supersedes the error scheduled
                    // for the same step, even when it is formatted later.
                    self.thrown = None;
                    self.formatting = None;
                    Some(error.into_any())
                }
                Poll::Pending => self.thrown.take(),
            };
            if thrown.is_none() {
                if let Some(formatting) = self.formatting.take() {
                    let result = formatting.lock().unwrap().take();
                    match result {
                        Some(Ok(error)) => thrown = Some(error),
                        Some(Err(error)) => return Poll::Ready(Err(error)),
                        None => { self.formatting = Some(formatting); return Poll::Pending; }
                    }
                }
            }
            if let Some(waiting) = self.waiting.as_ref() {
                if thrown.is_some() {
                    if let Err(error) = waiting.bind(py).call_method0("cancel") { return Poll::Ready(Err(error)); }
                } else {
                    match waiting.bind(py).call_method0("done").and_then(|v| v.is_truthy()) {
                        Ok(false) => return Poll::Pending,
                        Ok(true) => {},
                        Err(error) => return Poll::Ready(Err(error)),
                    }
                }
                self.waiting = None;
            }
            let result = if let Some(error) = thrown {
                match self.iterator.bind(py).hasattr("throw") {
                    Ok(true) => self.iterator.bind(py).call_method1("throw", (error,)),
                    Ok(false) => return Poll::Ready(Err(PyErr::from_value(error.into_bound(py)))),
                    Err(error) => return Poll::Ready(Err(error)),
                }
            } else {
                // Drive the iterator through CPython's send protocol, which
                // avoids observable instance lookup of __next__ and returns
                // completion values without constructing StopIteration.
                let mut output = std::ptr::null_mut();
                let status = unsafe { ffi::PyIter_Send(self.iterator.bind(py).as_ptr(), py.None().as_ptr(), &mut output) };
                match status {
                    ffi::PySendResult::PYGEN_RETURN => {
                        return Poll::Ready(unsafe { Bound::from_owned_ptr_or_err(py, output) }.map(Bound::unbind));
                    }
                    ffi::PySendResult::PYGEN_NEXT => unsafe { Bound::from_owned_ptr_or_err(py, output) },
                    ffi::PySendResult::PYGEN_ERROR => Err(PyErr::fetch(py)),
                }
            };
            let yielded = match result {
                Err(error) if error.is_instance_of::<PyStopIteration>(py) => {
                    return Poll::Ready(error.value(py).getattr("value").map(Bound::unbind));
                }
                Err(error) => return Poll::Ready(Err(error)),
                Ok(value) => value,
            };
            if yielded.is_none() {
                cx.waker().wake_by_ref();
                return Poll::Pending;
            }
            let setup = (|| -> PyResult<(Option<String>, Option<u8>, Option<Py<PyAny>>)> {
                let blocking = match yielded.getattr("_asyncio_future_blocking") {
                    Ok(value) => value,
                    Err(error) if error.is_instance_of::<PyAttributeError>(py) => py.None().into_bound(py),
                    Err(error) => return Err(error),
                };
                let asyncio = py.import("asyncio")?;
                if blocking.is_none() {
                    let generator = py.import("types")?.getattr("GeneratorType")?;
                    if yielded.is_instance(&generator)? {
                        let task = asyncio.call_method0("current_task")?;
                        return Ok((None, Some(3), Some(task.unbind())));
                    }
                    return Ok((Some(format!("Task got bad yield: {}", yielded.repr()?)), None, None));
                }
                let loop_ = match yielded.getattr("get_loop") {
                    Ok(method) => method.call0()?,
                    Err(error) if error.is_instance_of::<PyAttributeError>(py) => yielded.getattr("_loop")?,
                    Err(error) => return Err(error),
                };
                let task = asyncio.call_method0("current_task")?;
                if !loop_.is(&asyncio.call_method0("get_running_loop")?) {
                    return Ok((None, Some(0), Some(task.unbind())));
                }
                if !blocking.is_truthy()? {
                    return Ok((None, Some(1), Some(task.unbind())));
                }
                if yielded.is(&task) {
                    return Ok((None, Some(2), Some(task.unbind())));
                }
                // asyncio.Task normally clears this bit when a Future is yielded.
                yielded.setattr("_asyncio_future_blocking", false)?;
                let callback = Py::new(py, WakeCallback { wake: cx.waker().clone() })?;
                yielded.call_method1("add_done_callback", (callback,))?;
                Ok((None, None, None))
            })();
            match setup {
                Ok((Some(message), _, _)) => {
                    self.thrown = Some(PyRuntimeError::new_err(message).into_value(py).into_any());
                    cx.waker().wake_by_ref();
                    return Poll::Pending;
                }
                Ok((None, Some(mode), Some(task))) => {
                    let result = Arc::new(Mutex::new(None));
                    let callback = match Py::new(py, FormatErrorCallback { task, value: yielded.clone().unbind(), mode,
                        result: result.clone(), wake: cx.waker().clone() }) {
                        Ok(callback) => callback,
                        Err(error) => return Poll::Ready(Err(error)),
                    };
                    if let Err(error) = py.import("asyncio").and_then(|m| m.call_method0("get_running_loop"))
                        .and_then(|loop_| loop_.call_method1("call_soon", (callback,))) {
                        return Poll::Ready(Err(error));
                    }
                    self.formatting = Some(result);
                    return Poll::Pending;
                }
                Ok((None, None, None)) => {},
                Ok(_) => return Poll::Ready(Err(PyRuntimeError::new_err("invalid prototype setup"))),
                Err(error) => return Poll::Ready(Err(error)),
            }
            self.waiting = Some(yielded.unbind());
            Poll::Pending
        })
    }
}

struct AwaitGlobals { globals: Py<PyDict>, builtins: Py<PyDict> }
impl AwaitGlobals {
    fn new() -> PyResult<Self> {
        Python::attach(|py| {
            let function = py.import("jinja2.async_utils")?.getattr("auto_await")?;
            Ok(Self { globals: function.getattr("__globals__")?.cast_into::<PyDict>()?.unbind(),
                builtins: function.getattr("__builtins__")?.cast_into::<PyDict>()?.unbind() })
        })
    }
}

async fn call(callback: Py<PyAny>, globals: &AwaitGlobals, cancel: &mut CancelHandle) -> PyResult<Py<PyAny>> {
    let (value, iterator) = Python::attach(|py| -> PyResult<_> {
        let value = callback.bind(py).call0()?;
        // Mirror auto_await's live set membership, including replacement or
        // mutation by a user callback and errors from custom containers.
        let primitives = match globals.globals.bind(py).get_item("_common_primitives")? {
            Some(value) => Some(value),
            None => globals.builtins.bind(py).get_item("_common_primitives")?,
        }
            .ok_or_else(|| PyNameError::new_err("name '_common_primitives' is not defined"))?;
        if primitives.contains(value.get_type())? {
            return Ok((Some(value.unbind()), None));
        }
        let types = py.import("types")?;
        let generator = types.getattr("GeneratorType")?;
        if value.is_instance(&generator)? {
            let flags: u32 = value.getattr("gi_code")?.getattr("co_flags")?.extract()?;
            if flags & 0x100 != 0 { return Ok((None, Some(value.unbind()))); }
        }
        let coroutine = types.getattr("CoroutineType")?;
        let awaitable = py.import("collections.abc")?.getattr("Awaitable")?;
        if value.is_instance(&coroutine)? || value.is_instance(&awaitable)? {
            let iterator = await_slot_iterator(&value)?.unbind();
            Ok((None, Some(iterator)))
        } else { Ok((Some(value.unbind()), None)) }
    })?;
    match (value, iterator) {
        (Some(value), _) => Ok(value),
        (_, Some(iterator)) => DirectAwait { iterator, waiting: None, thrown: None, formatting: None, cancel }.await,
        _ => Err(PyRuntimeError::new_err("invalid prototype state")),
    }
}

#[pyfunction]
async fn run_direct(callbacks: Vec<Py<PyAny>>, #[pyo3(cancel_handle)] mut cancel: CancelHandle)
    -> PyResult<Vec<Py<PyAny>>> {
    let globals = AwaitGlobals::new()?;
    let mut result = Vec::with_capacity(callbacks.len());
    for callback in callbacks { result.push(call(callback, &globals, &mut cancel).await?); }
    Ok(result)
}

type AsyncSlot = unsafe extern "C" fn(*mut ffi::PyObject) -> *mut ffi::PyObject;
fn async_slot(value: &Bound<'_, PyAny>, slot: i32) -> Option<AsyncSlot> {
    // The GIL is held by Bound. Py_am_await, Py_am_aiter and Py_am_anext
    // have the unaryfunc signature; the slot pointer is borrowed.
    let pointer = unsafe { ffi::PyType_GetSlot(ffi::Py_TYPE(value.as_ptr()), slot) };
    if pointer.is_null() { None } else { Some(unsafe { std::mem::transmute(pointer) }) }
}

fn await_slot_iterator<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    let Some(slot) = async_slot(value, ffi::Py_am_await) else {
        let name = value.get_type().name()?.to_string();
        return Err(PyTypeError::new_err(format!("object {name} can't be used in 'await' expression")));
    };
    // The unary slot returns a new reference or sets a Python exception.
    let result = unsafe { Bound::from_owned_ptr_or_err(py, slot(value.as_ptr()))? };
    let generator_coroutine = if unsafe { ffi::PyGen_Check(result.as_ptr()) != 0 } {
        let flags: u32 = result.getattr("gi_code")?.getattr("co_flags")?.extract()?;
        flags & 0x100 != 0
    } else { false };
    if generator_coroutine || unsafe { ffi::PyCoro_CheckExact(result.as_ptr()) != 0 } {
        return Err(PyTypeError::new_err("__await__() returned a coroutine"));
    }
    if unsafe { ffi::PyIter_Check(result.as_ptr()) == 0 } {
        let name = result.get_type().name()?.to_string();
        return Err(PyTypeError::new_err(format!("__await__() returned non-iterator of type '{name}'")));
    }
    Ok(result)
}

// Laboratory primitive for the execution model needed by an async Jinja loop.
// It collects values; it is not connected to template parsing or rendering.
#[pyfunction]
async fn collect_iteration(iterable: Py<PyAny>, #[pyo3(cancel_handle)] mut cancel: CancelHandle)
    -> PyResult<Vec<Py<PyAny>>> {
    let (iterator, asynchronous) = Python::attach(|py| -> PyResult<_> {
        let iterable = iterable.bind(py);
        if iterable.hasattr("__aiter__")? {
            // Jinja auto_aiter first calls the object's method. The async-for
            // protocol then obtains the iterator through the type slot.
            let first = iterable.call_method0("__aiter__")?;
            let Some(aiter) = async_slot(&first, ffi::Py_am_aiter) else {
                let name = first.get_type().name()?.to_string();
                return Err(PyTypeError::new_err(format!("'async for' requires an object with __aiter__ method, got {name}")));
            };
            // The slot returns a new reference or a Python exception.
            let iterator = unsafe { Bound::from_owned_ptr_or_err(py, aiter(first.as_ptr()))? };
            if async_slot(&iterator, ffi::Py_am_anext).is_none() {
                let name = iterator.get_type().name()?.to_string();
                return Err(PyTypeError::new_err(format!("'async for' received an object from __aiter__ that does not implement __anext__: {name}")));
            }
            Ok((iterator.unbind(), true))
        } else {
            Ok((iterable.try_iter()?.into_any().unbind(), false))
        }
    })?;
    let mut values = Vec::new();
    loop {
        if !asynchronous {
            let next = Python::attach(|py| iterator.bind(py).call_method0("__next__").map(Bound::unbind));
            match next {
                Ok(value) => values.push(value),
                Err(error) if Python::attach(|py| error.is_instance_of::<PyStopIteration>(py)) => break,
                Err(error) => return Err(error),
            }
            continue;
        }
        let await_iterator = Python::attach(|py| -> PyResult<Py<PyAny>> {
            let iterator = iterator.bind(py);
            // Async-for dispatches through the type slot, independently of a
            // mutable builtins.anext. The GIL is held, iterator is alive, and
            // the slot's owned return is transferred into a PyO3 Bound.
            let Some(next) = async_slot(iterator, ffi::Py_am_anext) else {
                let name = iterator.get_type().name()?.to_string();
                return Err(PyTypeError::new_err(format!("'async for' requires an iterator with __anext__ method, got {name}")));
            };
            let value = unsafe { Bound::from_owned_ptr_or_err(py, next(iterator.as_ptr()))? };
            // CPython's generator check examines the actual type, as the
            // await protocol does, even if Python's types module is patched.
            if unsafe { ffi::PyGen_Check(value.as_ptr()) != 0 } {
                let flags: u32 = value.getattr("gi_code")?.getattr("co_flags")?.extract()?;
                if flags & 0x100 != 0 { return Ok(value.unbind()); }
            }
            match await_slot_iterator(&value) {
                Ok(iterator) => Ok(iterator.unbind()),
                Err(error) => {
                    let name = value.get_type().name()?.to_string();
                    let wrapped = PyTypeError::new_err(format!("'async for' received an invalid object from __anext__: {name}"));
                    wrapped.set_cause(py, Some(error));
                    Err(wrapped)
                }
            }
        });
        let await_iterator = match await_iterator {
            Ok(value) => value,
            Err(error) if Python::attach(|py| error.is_instance_of::<PyStopAsyncIteration>(py)) => break,
            Err(error) => return Err(error),
        };
        match (DirectAwait { iterator: await_iterator, waiting: None, thrown: None, formatting: None, cancel: &mut cancel }).await {
            Ok(value) => values.push(value),
            Err(error) if Python::attach(|py| error.is_instance_of::<PyStopAsyncIteration>(py)) => break,
            Err(error) => return Err(error),
        }
    }
    Ok(values)
}

enum Job {
    Invoke(Py<PyAny>, mpsc::Sender<PyResult<Py<PyAny>>>),
    Finished(PyResult<Vec<Py<PyAny>>>),
}
#[derive(Default)]
struct Queue { jobs: VecDeque<Job>, waker: Option<Waker>, closed: bool }
struct Shutdown(Arc<Mutex<Queue>>);
impl Drop for Shutdown {
    fn drop(&mut self) {
        let mut queue = self.0.lock().unwrap();
        queue.closed = true;
        queue.jobs.clear();
        queue.waker = None;
    }
}
fn enqueue(queue: &Arc<Mutex<Queue>>, job: Job) -> bool {
    let waker = {
        let mut queue = queue.lock().unwrap();
        if queue.closed { return false; }
        queue.jobs.push_back(job);
        queue.waker.take()
    };
    if let Some(waker) = waker { waker.wake(); }
    true
}

#[pyfunction(signature = (callbacks, delay_ms=0))]
async fn run_from_worker(callbacks: Vec<Py<PyAny>>, delay_ms: u64, #[pyo3(cancel_handle)] mut cancel: CancelHandle)
    -> PyResult<Vec<Py<PyAny>>> {
    let globals = AwaitGlobals::new()?;
    let queue = Arc::new(Mutex::new(Queue::default()));
    let _shutdown = Shutdown(queue.clone());
    let worker_queue = queue.clone();
    thread::spawn(move || {
        if delay_ms > 0 { thread::sleep(std::time::Duration::from_millis(delay_ms)); }
        let mut results = Vec::new();
        for callback in callbacks {
            let (sender, receiver) = mpsc::channel();
            if !enqueue(&worker_queue, Job::Invoke(callback, sender)) { return; }
            match receiver.recv() {
                Ok(Ok(value)) => results.push(value),
                Ok(Err(error)) => { let _ = enqueue(&worker_queue, Job::Finished(Err(error))); return; }
                Err(_) => return,
            }
        }
        let _ = enqueue(&worker_queue, Job::Finished(Ok(results)));
    });
    loop {
        let job = poll_fn(|cx| {
            let mut queue = queue.lock().unwrap();
            if let Poll::Ready(error) = cancel.poll_cancelled(cx) {
                return Poll::Ready(Err(Python::attach(|py| PyErr::from_value(error.into_bound(py)))));
            }
            if let Some(job) = queue.jobs.pop_front() { Poll::Ready(Ok(job)) }
            else { queue.waker = Some(cx.waker().clone()); Poll::Pending }
        }).await?;
        match job {
            Job::Invoke(callback, response) => {
                let _ = response.send(call(callback, &globals, &mut cancel).await);
            }
            Job::Finished(result) => return result,
        }
    }
}

#[pymodule]
fn _async_direct(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(run_direct, module)?)?;
    module.add_function(wrap_pyfunction!(run_from_worker, module)?)?;
    module.add_function(wrap_pyfunction!(collect_iteration, module)?)?;
    Ok(())
}
