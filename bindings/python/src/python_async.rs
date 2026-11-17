//! Same-thread suspension of the shared VM at foreign await boundaries.
//! Python's task drives the yielded objects and injects exceptions directly;
//! no nested event loop, worker thread or replacement asyncio scheduler.
use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::sync::Mutex;
use std::sync::Arc;
use corosensei::{Coroutine, CoroutineResult, Yielder};
use pyo3::exceptions::{PyAttributeError, PyGeneratorExit, PyRuntimeError, PyStopIteration, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use pyo3::ffi;
use crate::pyglobals::{frame_global, FrameGlobals};

struct CapturedPythonFunction { function: Py<PyAny>, code: Py<PyAny> }
static AUTO_AITER_FUNCTION: pyo3::sync::PyOnceLock<Option<CapturedPythonFunction>> = pyo3::sync::PyOnceLock::new();
static AUTO_AWAIT_FUNCTION: pyo3::sync::PyOnceLock<Option<CapturedPythonFunction>> = pyo3::sync::PyOnceLock::new();
static ISAWAITABLE_FUNCTION: pyo3::sync::PyOnceLock<Option<CapturedPythonFunction>> = pyo3::sync::PyOnceLock::new();
// AsyncLoopContext._to_iterator's function when the extension was loaded.
static ASYNC_TO_ITERATOR_FUNCTION: pyo3::sync::PyOnceLock<Option<CapturedPythonFunction>> = pyo3::sync::PyOnceLock::new();

fn capture_known_function(py: Python<'_>, module_name: &str, name: &str,
    function: &Bound<'_, PyAny>) -> PyResult<Option<CapturedPythonFunction>> {
    let Some(module) = crate::find_loaded_module(py, &PyString::new(py, module_name))? else { return Ok(None); };
    let Ok(module) = module.bind(py).cast::<pyo3::types::PyModule>() else { return Ok(None); };
    if !function.is_exact_instance_of::<pyo3::types::PyFunction>()
        || !function.getattr("__globals__")?.is(&module.dict()) { return Ok(None); }
    let code = function.getattr("__code__")?;
    let Some(filename) = module.dict().get_item("__file__")? else { return Ok(None); };
    let code_file = code.getattr("co_filename")?;
    let code_name = code.getattr("co_name")?;
    if !filename.is_exact_instance_of::<PyString>() || !code_file.is_exact_instance_of::<PyString>()
        || !code_name.is_exact_instance_of::<PyString>() || !filename.eq(&code_file)? || !code_name.eq(name)? {
        return Ok(None);
    }
    Ok(Some(CapturedPythonFunction { function: function.clone().unbind(), code: code.unbind() }))
}

fn capture_loaded_function(py: Python<'_>, module_name: &str, name: &str) -> PyResult<Option<CapturedPythonFunction>> {
    let Some(module) = crate::find_loaded_module(py, &PyString::new(py, module_name))? else { return Ok(None); };
    let Ok(module) = module.bind(py).cast::<pyo3::types::PyModule>() else { return Ok(None); };
    let Some(function) = module.dict().get_item(name)? else { return Ok(None); };
    capture_known_function(py, module_name, name, &function)
}

pub(crate) fn initialize(py: Python<'_>) -> PyResult<()> {
    AUTO_AITER_FUNCTION.get_or_try_init(py, || capture_loaded_function(py, "jinja2.async_utils", "auto_aiter"))?;
    AUTO_AWAIT_FUNCTION.get_or_try_init(py, || capture_loaded_function(py, "jinja2.async_utils", "auto_await"))?;
    ISAWAITABLE_FUNCTION.get_or_try_init(py, || capture_loaded_function(py, "inspect", "isawaitable"))?;
    ASYNC_TO_ITERATOR_FUNCTION.get_or_try_init(py, || -> PyResult<Option<CapturedPythonFunction>> {
        let Some(module) = crate::find_loaded_module(py, &PyString::new(py, "jinja2.runtime"))? else { return Ok(None); };
        let Some(class) = module.bind(py).cast::<pyo3::types::PyModule>()?.dict().get_item("AsyncLoopContext")? else { return Ok(None); };
        let Ok(member) = class.getattr("__dict__")?.get_item("_to_iterator") else { return Ok(None); };
        let Ok(function) = member.getattr("__func__") else { return Ok(None); };
        if !function.is_exact_instance_of::<pyo3::types::PyFunction>() { return Ok(None); }
        Ok(Some(CapturedPythonFunction { code: function.getattr("__code__")?.unbind(), function: function.unbind() }))
    })?;
    Ok(())
}

/// Whether `function` is LoopContext.__init__ as the bridge module recorded
/// it when it was imported; without that record it is taken as the original.
pub(crate) fn is_original_loop_constructor(function: &Bound<'_, PyAny>) -> PyResult<bool> {
    let binding = crate::python_function::binding(function.py())?;
    let original = binding.getattr(pyo3::intern!(function.py(), "_RUNTIME_LOOP_INIT"))?;
    let code = binding.getattr(pyo3::intern!(function.py(), "_RUNTIME_LOOP_INIT_CODE"))?;
    if original.is_none() || code.is_none() { return Ok(true); }
    Ok(function.is(&original) && function.getattr(pyo3::intern!(function.py(), "__code__"))?.is(&code))
}

/// Whether `function` is AsyncLoopContext._to_iterator's function, with its
/// code, as when the extension was loaded; an exact code clone is not.
pub(crate) fn is_original_async_to_iterator(function: &Bound<'_, PyAny>) -> PyResult<bool> {
    match ASYNC_TO_ITERATOR_FUNCTION.get(function.py()).and_then(Option::as_ref) {
        Some(captured) => captured_matches(function, captured),
        None => Ok(false),
    }
}

pub(crate) fn is_standard_auto_aiter(function: &Bound<'_, PyAny>) -> PyResult<bool> {
    match AUTO_AITER_FUNCTION.get(function.py()).and_then(Option::as_ref) {
        Some(captured) => captured_matches(function, captured),
        None => Ok(false),
    }
}

fn captured_matches(function: &Bound<'_, PyAny>, captured: &CapturedPythonFunction) -> PyResult<bool> {
    Ok(function.is(captured.function.bind(function.py())) && function.getattr("__code__")?.is(captured.code.bind(function.py())))
}

fn global<'py>(scope: &FrameGlobals<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    frame_global(scope, &PyString::new(scope.py(), name))
}

fn cast_value<'py>(scope: &FrameGlobals<'py>, name: &str, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let cast = global(scope, "t")?.getattr("cast")?;
    if crate::native_filters::is_standard_cast(&cast)? { Ok(value.clone()) } else { cast.call1((name, value)) }
}

fn standard_isawaitable<'py>(function: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let scope = FrameGlobals::of_function(function)?;
    let instance = |name: &str| -> PyResult<Bound<'py, PyAny>> {
        let callback = global(&scope, "isinstance")?;
        let kind = global(&scope, "types")?.getattr(name)?;
        callback.call1((value, kind))
    };
    let coroutine = instance("CoroutineType")?;
    if coroutine.is_truthy()? { return Ok(coroutine); }
    let generator = instance("GeneratorType")?;
    let candidate = if generator.is_truthy()? {
        let boolean = global(&scope, "bool")?;
        let flags = value.getattr("gi_code")?.getattr("co_flags")?;
        let iterable = global(&scope, "CO_ITERABLE_COROUTINE")?;
        let both = unsafe { Bound::from_owned_ptr_or_err(value.py(), ffi::PyNumber_And(flags.as_ptr(), iterable.as_ptr()))? };
        boolean.call1((both,))?
    } else { generator };
    if candidate.is_truthy()? { return Ok(candidate); }
    let callback = global(&scope, "isinstance")?;
    let kind = global(&scope, "collections")?.getattr("abc")?.getattr("Awaitable")?;
    callback.call1((value, kind))
}

enum Resume {
    Value(Py<PyAny>),
    Error(PyErr),
    Throw(Py<PyTuple>),
}
type Suspend = Yielder<Resume, Py<PyAny>>;
type PendingRender = Box<dyn FnOnce() -> PyResult<Py<PyAny>> + Send>;
struct Driver {
    yielder: Cell<*const Suspend>,
    render_frames: RefCell<Option<Vec<minijinja::RuntimeRenderFrame>>>,
    filtered_loop_depth: Cell<Option<usize>>,
    deferred_cleanup: RefCell<Option<Py<PyAny>>>,
    // The Python frame on top of the stack when the current step resumed the
    // fiber; a suspension under another frame has Python frames on the fiber.
    entry_frame: Cell<*mut ffi::PyFrameObject>,
    // `drive` calls active on the fiber, and host methods that run template
    // code (`pinned`): a suspension inside either is under a host call.
    drive_depth: Cell<usize>,
    pins: Cell<usize>,
    // Error scopes open on the thread when the step resumed the fiber: a
    // scope the fiber opened is per thread and must close where it opened.
    entry_scopes: Cell<usize>,
    // Whether the fiber's current suspension may resume on another thread.
    migratable: Cell<bool>,
}

/// Run `f`, a host method that runs template code or awaits on the fiber
/// under its own call: a suspension inside it keeps the fiber on its thread.
pub(crate) fn pinned<R>(f: impl FnOnce() -> R) -> R {
    struct Unpin<'a>(&'a Driver);
    impl Drop for Unpin<'_> {
        fn drop(&mut self) { self.0.pins.set(self.0.pins.get() - 1); }
    }
    let Some(driver) = current_driver() else { return f(); };
    driver.pins.set(driver.pins.get() + 1);
    let _unpin = Unpin(driver);
    f()
}
// The driver of the render fiber running on this thread, installed by each
// step; and the driver whose fiber must not suspend (a host callback borrows
// its state). Both point to a `Driver` owned by the running `RenderFiber`.
crate::fiber_local::fiber_slot!(static CURRENT);
crate::fiber_local::fiber_slot!(static NO_REENTRANT_SUSPEND);

/// The driver of the fiber running on this thread.
fn current_driver() -> Option<&'static Driver> {
    let pointer = CURRENT.get().cast::<Driver>();
    // SAFETY: a step installs the pointer for the duration of the resume,
    // while its RenderFiber owns the driver; callers do not keep it.
    (!pointer.is_null()).then(|| unsafe { &*pointer })
}

/// Allow a nested callback on the active render stack only while it completes
/// without suspending that same fiber. A separate nested render has its own
/// driver and is not restricted by this scope.
/// Whether the running render cannot suspend here (a host callback borrows
/// its state).
pub(crate) fn reentrant_suspension_forbidden() -> bool {
    let current = CURRENT.get();
    !current.is_null() && NO_REENTRANT_SUSPEND.get() == current
}

/// Whether a native render runs on this thread (host callbacks of it run on
/// its stack).
pub(crate) fn rendering() -> bool { CURRENT.is_set() }

pub(crate) fn without_reentrant_suspension<R>(call: impl FnOnce() -> R) -> R {
    let current = CURRENT.get();
    if current.is_null() { call() } else { NO_REENTRANT_SUSPEND.set(current, call) }
}

/// Run `call` with no reentrancy guard installed (see `fiber_local::isolate`).
pub(crate) fn without_reentrancy_guard<R>(call: impl FnOnce() -> R) -> R {
    NO_REENTRANT_SUSPEND.set(std::ptr::null(), call)
}

struct RenderFramesGuard<'a> {
    driver: &'a Driver,
    previous: Option<Vec<minijinja::RuntimeRenderFrame>>,
    previous_filtered_loop_depth: Option<usize>,
}

impl Drop for RenderFramesGuard<'_> {
    fn drop(&mut self) {
        self.driver.render_frames.replace(self.previous.take());
        self.driver.filtered_loop_depth.set(self.previous_filtered_loop_depth);
    }
}

pub(crate) fn with_render_frames<R>(frames: Vec<minijinja::RuntimeRenderFrame>, filtered_loop_depth: usize,
    call: impl FnOnce() -> R) -> R {
    let Some(driver) = current_driver() else { return call(); };
    let _guard = RenderFramesGuard { driver, previous: driver.render_frames.replace(Some(frames)),
        previous_filtered_loop_depth: driver.filtered_loop_depth.replace(Some(filtered_loop_depth)) };
    call()
}

#[pyfunction]
fn _debug_filtered_loop_depth() -> Option<usize> {
    current_driver().and_then(|driver| driver.filtered_loop_depth.get())
}

#[pyfunction]
fn _debug_render_frame_depth() -> Option<usize> {
    current_driver().and_then(|driver| driver.render_frames.borrow().as_ref().map(Vec::len))
}

#[pyfunction]
fn _debug_render_frames() -> Option<Vec<(usize, &'static str, String)>> {
    current_driver().and_then(|driver| driver.render_frames.borrow().as_ref().map(|frames| frames.iter().map(|frame| {
        let kind = match frame.kind {
            minijinja::RuntimeRenderFrameKind::Root => "root",
            minijinja::RuntimeRenderFrameKind::Include => "include",
            minijinja::RuntimeRenderFrameKind::Inheritance => "inheritance",
            minijinja::RuntimeRenderFrameKind::Block => "block",
            minijinja::RuntimeRenderFrameKind::Super => "super",
        };
        (frame.token, kind, frame.template.clone())
    }).collect()))
}

#[pyclass(module = "docxtpl_native._native")]
struct RenderFiber {
    fiber: std::mem::ManuallyDrop<Coroutine<Resume, Py<PyAny>, PyResult<Py<PyAny>>>>,
    driver: Rc<Driver>,
    started: bool,
    bridge: Arc<crate::python_bridge::Bridge>,
    error_slot: crate::python_bridge::LocalErrorSlot,
    // The thread of the last suspension.
    suspended_on: Option<std::thread::ThreadId>,
}

// SAFETY: a RenderFiber is only used through its Python object, under the
// GIL, by one thread at a time (RenderCoroutine borrows it mutably for a
// step, and refuses a concurrent step). Its `Rc`s (the driver, the error
// slot) are cloned and dropped only inside those exclusive steps. Its
// stack moves to another thread only at a suspension the driver marked
// migratable (`drive`): the outermost await of the fiber, not under a host
// call or a Python frame, so no pyo3 attach guard, Python frame or borrowed
// host callback is live on the stack. Thread-locals that code on the stack
// reads or restores after a resume are accessed through `fiber_local`, which
// computes their address on the resuming thread, and each step resumes the
// fiber with them empty. Other suspensions keep the fiber on its thread: a
// step or a drop on another thread refuses to run it.
unsafe impl Send for RenderFiber {}
unsafe impl Sync for RenderFiber {}

impl RenderFiber {
    fn new(bridge: Arc<crate::python_bridge::Bridge>, render: impl FnOnce() -> PyResult<Py<PyAny>> + 'static) -> Self {
        let driver = Rc::new(Driver { yielder: Cell::new(std::ptr::null()), render_frames: RefCell::new(None),
            filtered_loop_depth: Cell::new(None),
            deferred_cleanup: RefCell::new(None), entry_frame: Cell::new(std::ptr::null_mut()),
            drive_depth: Cell::new(0), pins: Cell::new(0), entry_scopes: Cell::new(0), migratable: Cell::new(false) });
        let local = driver.clone();
        let fiber = Coroutine::new(move |yielder, first: Resume| {
            local.yielder.set(yielder);
            match first {
                Resume::Value(_) => {},
                Resume::Error(error) => return Err(error),
                Resume::Throw(_) => return Err(PyRuntimeError::new_err("unexpected unnormalized initial throw")),
            }
            render()
        });
        Self { fiber: std::mem::ManuallyDrop::new(fiber), driver, started: false, bridge,
            error_slot: Rc::new(std::cell::RefCell::new(None)), suspended_on: None }
    }

    /// Run `operation` on the fiber from this thread: its slots start empty,
    /// and this step's Python frame is the one a clean suspension returns to.
    fn enter<R>(&mut self, operation: impl FnOnce(&mut Coroutine<Resume, Py<PyAny>, PyResult<Py<PyAny>>>) -> R) -> R {
        // SAFETY: the GIL is held; the frame pointer is only compared.
        self.driver.entry_frame.set(unsafe { ffi::PyEval_GetFrame() });
        let bridge = self.bridge.clone();
        let slot = self.error_slot.clone();
        let driver = Rc::as_ptr(&self.driver).cast::<()>();
        let fiber = &mut *self.fiber;
        let entry_scopes = &self.driver.entry_scopes;
        crate::fiber_local::isolate(|| bridge.with_error_slot(&slot, || {
            entry_scopes.set(crate::python_bridge::error_scope_depth());
            CURRENT.set(driver, || operation(fiber))
        }))
    }

    /// Whether the suspended stack may run on this thread.
    fn runs_here(&self) -> bool {
        self.suspended_on.is_none_or(|owner| owner == std::thread::current().id()) || self.driver.migratable.get()
    }

    fn step(&mut self, py: Python<'_>, input: Resume) -> PyResult<Py<PyAny>> {
        if self.fiber.done() { return Err(PyRuntimeError::new_err("cannot reuse already awaited coroutine")); }
        if !self.started && matches!(&input, Resume::Value(value) if !value.bind(py).is_none()) {
            return Err(PyTypeError::new_err("can't send non-None value to a just-started coroutine"));
        }
        self.started = true;
        match self.enter(|fiber| fiber.resume(input)) {
            CoroutineResult::Yield(value) => {
                self.suspended_on = Some(std::thread::current().id());
                Ok(value)
            }
            CoroutineResult::Return(Ok(value)) => Err(PyStopIteration::new_err((value,))),
            CoroutineResult::Return(Err(error)) => Err(crate::python_errors::escaped_iteration(
                py, error, crate::python_errors::Boundary::Coroutine)) ,
        }
    }
}

impl Drop for RenderFiber {
    fn drop(&mut self) {
        if !self.fiber.done() && !self.runs_here() {
            // A stack bound to its thread cannot unwind here; it is leaked.
            Python::try_attach(|py| PyRuntimeError::new_err(
                "a suspended native render bound to its thread was collected on another thread")
                .write_unraisable(py, None));
            return;
        }
        self.enter(|fiber| fiber.force_unwind());
        // SAFETY: dropped once, here; the stack has unwound.
        unsafe { std::mem::ManuallyDrop::drop(&mut self.fiber) };
    }
}

#[pyclass(module = "docxtpl_native._native")]
pub(crate) struct RenderCoroutine {
    fiber: Option<Py<RenderFiber>>,
    pending: Mutex<Option<PendingRender>>,
    bridge: Option<Arc<crate::python_bridge::Bridge>>,
    deferred_cleanup: Option<Py<PyAny>>,
}

impl RenderCoroutine {
    fn borrow_running<'py>(slf: &'py Bound<'_, Self>) -> PyResult<PyRefMut<'py, Self>> {
        slf.try_borrow_mut().map_err(|_| PyValueError::new_err("coroutine already executing"))
    }

    fn new(bridge: Arc<crate::python_bridge::Bridge>, render: impl FnOnce() -> PyResult<Py<PyAny>> + Send + 'static) -> Self {
        Self { fiber: None, pending: Mutex::new(Some(Box::new(render))),
            bridge: Some(bridge), deferred_cleanup: None }
    }

    fn finalize_deferred(&mut self, py: Python<'_>) -> bool {
        let Some(iterator) = self.deferred_cleanup.take() else { return false; };
        let iterator = iterator.bind(py);
        close_unraisably(py, iterator);
        true
    }

    fn step(&mut self, py: Python<'_>, input: Resume) -> PyResult<Py<PyAny>> {
        if self.finalize_deferred(py) {
            return Err(PyRuntimeError::new_err("cannot reuse already awaited __anext__()/asend()"));
        }
        let pending = self.pending.get_mut().expect("pending render mutex poisoned");
        if pending.is_some() && matches!(&input, Resume::Value(value) if !value.bind(py).is_none()) {
            return Err(PyTypeError::new_err("can't send non-None value to a just-started coroutine"));
        }
        if let Some(render) = pending.take() {
            let bridge = self.bridge.take().expect("pending render has bridge");
            self.fiber = Some(Py::new(py, RenderFiber::new(bridge, render))?);
        }
        if self.fiber.as_ref().is_some_and(|inner| !inner.bind(py).borrow().runs_here()) {
            return Err(crate::python_bridge::unsupported(py,
                "a suspended native render cannot yet resume on another thread from this await"));
        }
        let Some(inner) = &self.fiber else {
            return Err(PyRuntimeError::new_err("cannot reuse already awaited coroutine"));
        };
        let (result, done, deferred_cleanup) = {
            let mut inner = inner.bind(py).borrow_mut();
            let result = inner.step(py, input);
            let done = inner.fiber.done();
            let deferred_cleanup = if done { inner.driver.deferred_cleanup.borrow_mut().take() } else { None };
            (result, done, deferred_cleanup)
        };
        if done {
            self.deferred_cleanup = deferred_cleanup;
            // A completed stack no longer needs thread-affine storage. Release
            // it here, before its Python handle can travel to another thread.
            self.fiber.take();
        }
        result
    }
}

impl Drop for RenderCoroutine {
    fn drop(&mut self) {
        if self.deferred_cleanup.is_some() {
            Python::try_attach(|py| { self.finalize_deferred(py); });
        }
    }
}

fn validate_throw(args: &Bound<'_, PyTuple>) -> PyResult<()> {
    let typ = args.get_item(0)?;
    let value = if args.len() > 1 { Some(args.get_item(1)?) } else { None };
    let value = value.as_ref().filter(|value| !value.is_none());
    let traceback = if args.len() > 2 { Some(args.get_item(2)?) } else { None };
    let traceback = traceback.as_ref().filter(|traceback| !traceback.is_none());
    if let Some(traceback) = traceback {
        if unsafe { ffi::PyTraceBack_Check(traceback.as_ptr()) } == 0 {
            return Err(PyTypeError::new_err("throw() third argument must be a traceback object"));
        }
    }
    if unsafe { ffi::PyExceptionInstance_Check(typ.as_ptr()) } != 0 {
        if value.is_some() {
            return Err(PyTypeError::new_err("instance exception may not have a separate value"));
        }
    } else if unsafe { ffi::PyExceptionClass_Check(typ.as_ptr()) } == 0 {
        return Err(PyTypeError::new_err(format!(
            "exceptions must be classes or instances deriving from BaseException, not {}", typ.get_type().name()?
        )));
    }
    Ok(())
}

fn normalize_throw(args: &Bound<'_, PyTuple>) -> PyResult<PyErr> {
    validate_throw(args)?;
    let typ = args.get_item(0)?;
    let value = if args.len() > 1 { Some(args.get_item(1)?) } else { None };
    let value = value.as_ref().filter(|value| !value.is_none());
    let traceback = if args.len() > 2 { Some(args.get_item(2)?) } else { None };
    let traceback = traceback.as_ref().filter(|traceback| !traceback.is_none());
    let instance = if unsafe { ffi::PyExceptionInstance_Check(typ.as_ptr()) } != 0 {
        typ.clone()
    } else {
        match value {
            Some(value) if unsafe { ffi::PyExceptionInstance_Check(value.as_ptr()) } != 0
                && unsafe { ffi::PyErr_GivenExceptionMatches(value.as_ptr(), typ.as_ptr()) } != 0 => value.clone(),
            Some(value) => typ.call1((value,))?,
            None => typ.call0()?,
        }
    };
    if unsafe { ffi::PyExceptionInstance_Check(instance.as_ptr()) } == 0 {
        return Err(PyTypeError::new_err(format!(
            "calling {} should have returned an instance of BaseException, not {}", typ.repr()?, instance.get_type().name()?
        )));
    }
    if let Some(traceback) = traceback {
        if unsafe { ffi::PyException_SetTraceback(instance.as_ptr(), traceback.as_ptr()) } != 0 {
            return Err(PyErr::fetch(args.py()));
        }
    }
    Ok(PyErr::from_value(instance))
}

#[pymethods]
impl RenderCoroutine {
    fn __await__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }
    fn __next__(slf: &Bound<'_, Self>, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Self::borrow_running(slf)?.step(py, Resume::Value(py.None()))
    }
    fn send(slf: &Bound<'_, Self>, py: Python<'_>, value: Py<PyAny>) -> PyResult<Py<PyAny>> {
        Self::borrow_running(slf)?.step(py, Resume::Value(value))
    }
    #[pyo3(signature = (*args))]
    fn throw(slf: &Bound<'_, Self>, py: Python<'_>, args: &Bound<'_, PyTuple>) -> PyResult<Py<PyAny>> {
        if args.is_empty() { return Err(PyTypeError::new_err("throw expected at least 1 argument, got 0")); }
        if args.len() > 3 { return Err(PyTypeError::new_err(format!("throw expected at most 3 arguments, got {}", args.len()))); }
        #[cfg(Py_3_12)]
        if args.len() > 1 {
            let message = c"the (type, exc, tb) signature of throw() is deprecated, use the single-arg signature instead.";
            if unsafe { ffi::PyErr_WarnEx(py.get_type::<pyo3::exceptions::PyDeprecationWarning>().as_ptr(), message.as_ptr(), 1) } != 0 {
                return Err(PyErr::fetch(py));
            }
        }
        let mut coroutine = Self::borrow_running(slf)?;
        if coroutine.fiber.is_some() || coroutine.deferred_cleanup.is_some() {
            // A deferred asend is already exhausted. CPython checks the
            // GeneratorExit signature before attempting to reuse it, but
            // other exceptions are not normalized or constructed first.
            if coroutine.deferred_cleanup.is_some() {
                let typ = args.get_item(0)?;
                if unsafe { ffi::PyErr_GivenExceptionMatches(typ.as_ptr(), py.get_type::<PyGeneratorExit>().as_ptr()) } != 0 {
                    validate_throw(args)?;
                }
            }
            coroutine.step(py, Resume::Throw(args.clone().unbind()))
        } else {
            validate_throw(args)?;
            let error = match normalize_throw(args) { Ok(error) | Err(error) => error };
            coroutine.step(py, Resume::Error(error))
        }
    }
    fn close(slf: &Bound<'_, Self>, py: Python<'_>) -> PyResult<()> {
        let mut coroutine = Self::borrow_running(slf)?;
        if coroutine.finalize_deferred(py) { return Ok(()); }
        if coroutine.pending.get_mut().expect("pending render mutex poisoned").take().is_some() {
            coroutine.bridge.take();
            return Ok(());
        }
        if coroutine.fiber.is_none() { return Ok(()); }
        match coroutine.step(py, Resume::Error(PyGeneratorExit::new_err(()))) {
            Err(error) if error.is_instance_of::<PyGeneratorExit>(py) || error.is_instance_of::<PyStopIteration>(py) => Ok(()),
            Err(error) => Err(error),
            Ok(_) => Err(PyRuntimeError::new_err("coroutine ignored GeneratorExit")),
        }
    }

    fn __traverse__(&self, visit: pyo3::gc::PyVisit<'_>) -> Result<(), pyo3::gc::PyTraverseError> {
        if let Some(iterator) = &self.deferred_cleanup { visit.call(iterator)?; }
        Ok(())
    }

    fn __clear__(&mut self) {
        if self.deferred_cleanup.is_some() {
            Python::try_attach(|py| { self.finalize_deferred(py); });
        }
    }
}

pub(crate) fn register(module: &Bound<'_, pyo3::types::PyModule>) -> PyResult<()> {
    module.add_class::<RenderCoroutine>()?;
    module.add_function(wrap_pyfunction!(_debug_render_frame_depth, module)?)?;
    module.add_function(wrap_pyfunction!(_debug_filtered_loop_depth, module)?)?;
    module.add_function(wrap_pyfunction!(_debug_render_frames, module)?)
}

/// A native coroutine running `render` in its own fiber when it is awaited.
pub(crate) fn native_coroutine(py: Python<'_>, bridge: Arc<crate::python_bridge::Bridge>,
    render: impl FnOnce() -> PyResult<Py<PyAny>> + Send + 'static) -> PyResult<Py<PyAny>> {
    Ok(Py::new(py, RenderCoroutine::new(bridge, render))?.into_any())
}

pub(crate) fn run(py: Python<'_>, bridge: Arc<crate::python_bridge::Bridge>, render: impl FnOnce() -> PyResult<Py<PyAny>> + Send + 'static) -> PyResult<Py<PyAny>> {
    let coroutine = Py::new(py, RenderCoroutine::new(bridge, render))?;
    // The binding trampoline preserves Task.get_coro()'s CPython type and
    // running/closed state. VM frames and await dispatch stay in this module.
    let host = py.import("docxtpl_native.bridge")?.getattr("_await_native_render")?.call1((coroutine,))?;
    // Jinja hands Template.render_async's coroutine to asyncio.run. Its public
    // names also appear in unawaited-coroutine warnings when run rejects it.
    host.setattr("__name__", "render_async")?;
    host.setattr("__qualname__", "Template.render_async")?;
    Ok(py.import("asyncio")?.call_method1("run", (host,))?.unbind())
}

type AsyncSlot = unsafe extern "C" fn(*mut ffi::PyObject) -> *mut ffi::PyObject;
pub(crate) fn async_slot(value: &Bound<'_, PyAny>, slot: i32) -> Option<AsyncSlot> {
    // The async slots have unaryfunc's signature. The GIL is held and value owns
    // a reference to the type for the duration of each immediate slot call.
    let pointer = unsafe { ffi::PyType_GetSlot(ffi::Py_TYPE(value.as_ptr()), slot) };
    if pointer.is_null() { None } else { Some(unsafe { std::mem::transmute(pointer) }) }
}

fn generator_coroutine(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    if unsafe { ffi::PyGen_Check(value.as_ptr()) == 0 } { return Ok(false); }
    Ok(value.getattr("gi_code")?.getattr("co_flags")?.extract::<u32>()? & 0x100 != 0)
}

pub(crate) fn async_for_iterator(value: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let py = value.py();
    let Some(slot) = async_slot(value, ffi::Py_am_aiter) else {
        return Err(PyTypeError::new_err(format!("'async for' requires an object with __aiter__ method, got {}", value.get_type().name()?)));
    };
    // GET_AITER calls the type slot and validates its owned result immediately.
    let iterator = unsafe { Bound::from_owned_ptr_or_err(py, slot(value.as_ptr()))? };
    if async_slot(&iterator, ffi::Py_am_anext).is_none() {
        return Err(PyTypeError::new_err(format!("'async for' received an object from __aiter__ that does not implement __anext__: {}", iterator.get_type().name()?)));
    }
    Ok(iterator.unbind())
}

pub(crate) fn await_iterator(value: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    if generator_coroutine(value)? { return Ok(value.clone().unbind()); }
    let py = value.py();
    let Some(slot) = async_slot(value, ffi::Py_am_await) else {
        return Err(PyTypeError::new_err(format!("object {} can't be used in 'await' expression", value.get_type().name()?)));
    };
    // The slot returns a new reference or leaves the original exception set.
    let iterator = unsafe { Bound::from_owned_ptr_or_err(py, slot(value.as_ptr()))? };
    if generator_coroutine(&iterator)? || unsafe { ffi::PyCoro_CheckExact(iterator.as_ptr()) != 0 } {
        return Err(PyTypeError::new_err("__await__() returned a coroutine"));
    }
    if unsafe { ffi::PyIter_Check(iterator.as_ptr()) == 0 } {
        return Err(PyTypeError::new_err(format!("__await__() returned non-iterator of type '{}'", iterator.get_type().name()?)));
    }
    Ok(iterator.unbind())
}

pub(crate) fn is_standard_auto_await(py: Python<'_>, selected: Option<&Py<PyAny>>) -> PyResult<bool> {
    let Some(selected) = selected else { return Ok(true); };
    match AUTO_AWAIT_FUNCTION.get(py).and_then(Option::as_ref) {
        Some(original) => captured_matches(selected.bind(py), original),
        None => Ok(capture_known_function(py, "jinja2.async_utils", "auto_await", selected.bind(py))?.is_some()),
    }
}

enum PreparedAwait { Ready(Py<PyAny>), Iterator(Py<PyAny>) }

pub(crate) fn auto_await(value: Py<PyAny>, selected: Option<Py<PyAny>>, adapter_probe: bool) -> PyResult<Py<PyAny>> {
    // Nothing of this activation runs after `drive` may have resumed it on
    // another thread: the arguments are consumed before (see `fiber_local`).
    match prepare_await(value, selected, adapter_probe)? {
        PreparedAwait::Iterator(iterator) => drive(iterator),
        PreparedAwait::Ready(value) => Ok(value),
    }
}

#[inline(never)]
fn prepare_await(value: Py<PyAny>, selected: Option<Py<PyAny>>, adapter_probe: bool) -> PyResult<PreparedAwait> {
    Python::attach(|py| -> PyResult<PreparedAwait> {
        let object = value.bind(py);
        let recovered;
        let original = match AUTO_AWAIT_FUNCTION.get(py).and_then(Option::as_ref) {
            Some(original) => Some(original),
            None => {
                recovered = if let Some(selected) = &selected {
                    capture_known_function(py, "jinja2.async_utils", "auto_await", selected.bind(py))?
                } else { capture_loaded_function(py, "jinja2.async_utils", "auto_await")? };
                recovered.as_ref()
            }
        };
        if let Some(original) = original {
            let function = selected.as_ref().map(|value| value.bind(py)).unwrap_or_else(|| original.function.bind(py));
            if !captured_matches(function, original)? {
                // Replaced functions and in-place replacements are user callbacks.
                // If the changed object still has standard provenance, it may
                // be a reloaded or cloned standard algorithm: do not execute
                // an unverified standard body through Python.
                if capture_known_function(py, "jinja2.async_utils", "auto_await", function)?.is_some() {
                    return Err(crate::python_bridge::unsupported(py,
                        "cannot verify changed standard auto_await provenance"));
                }
                return await_iterator(&function.call1((object,))?).map(PreparedAwait::Iterator);
            }
            let scope = FrameGlobals::of_function(function)?;
            let kind = global(&scope, "type")?.call1((object,))?;
            if global(&scope, "_common_primitives")?.contains(&kind)? {
                return Ok(PreparedAwait::Ready(cast_value(&scope, "V", object)?.unbind()));
            }
            let inspector = global(&scope, "inspect")?;
            let predicate = inspector.getattr("isawaitable")?;
            let recovered;
            let original = match ISAWAITABLE_FUNCTION.get(py).and_then(Option::as_ref) {
                Some(original) => Some(original),
                None => {
                    recovered = capture_loaded_function(py, "inspect", "isawaitable")?;
                    recovered.as_ref()
                }
            };
            let awaitable = if let Some(original) = original {
                if captured_matches(&predicate, original)? { standard_isawaitable(&predicate, object)?.is_truthy()? }
                else {
                    if capture_known_function(py, "inspect", "isawaitable", &predicate)?.is_some() {
                        return Err(crate::python_bridge::unsupported(py,
                            "cannot verify changed standard inspect.isawaitable provenance"));
                    }
                    predicate.call1((object,))?.is_truthy()?
                }
            } else {
                // Without a trusted baseline, never silently run a possible
                // standard inspect algorithm through the Python binding.
                let looks_standard = if let Ok(module) = inspector.cast::<pyo3::types::PyModule>() {
                    predicate.is_exact_instance_of::<pyo3::types::PyFunction>()
                        && predicate.getattr("__globals__")?.is(&module.dict())
                        && predicate.getattr("__code__")?.getattr("co_name")?.eq("isawaitable")?
                } else { false };
                if looks_standard {
                    return Err(crate::python_bridge::unsupported(py,
                        "cannot verify standard inspect.isawaitable provenance"));
                }
                predicate.call1((object,))?.is_truthy()?
            };
            if !awaitable { return Ok(PreparedAwait::Ready(value)); }
            let cast = cast_value(&scope, "t.Awaitable[V]", object)?;
            if adapter_probe {
                let adapter = py.import("docxtpl_native.bridge")?.getattr("_await_native_value")?.call1((&cast,))?;
                return await_iterator(&adapter).map(PreparedAwait::Iterator);
            }
            return await_iterator(&cast).map(PreparedAwait::Iterator);
        }
        if let Some(selected) = &selected {
            // A callback whose Python code is demonstrably not the standard
            // body must still run when the extension first loaded after a
            // caller mutation. Unknown provenance remains conservative below.
            let selected = selected.bind(py);
            let clearly_custom = if !selected.is_exact_instance_of::<pyo3::types::PyFunction>() {
                true
            } else if !selected.getattr("__code__")?.getattr("co_name")?.eq("auto_await")? {
                true
            } else if let Some(module) = crate::find_loaded_module(py, pyo3::intern!(py, "jinja2.async_utils"))? {
                if let Ok(module) = module.bind(py).cast::<pyo3::types::PyModule>() {
                    if !selected.getattr("__globals__")?.is(&module.dict()) { true }
                    else if let Some(file) = module.dict().get_item("__file__")? {
                        let filename = selected.getattr("__code__")?.getattr("co_filename")?;
                        if file.is_exact_instance_of::<PyString>() && filename.is_exact_instance_of::<PyString>() {
                            filename.ne(file)?
                        } else { false }
                    } else { false }
                } else { false }
            } else { false };
            if clearly_custom {
                return await_iterator(&selected.call1((object,))?).map(PreparedAwait::Iterator);
            }
        }
        // Preserve the existing guarded path if standard functions were not
        // available for capture at extension initialization.
        if object.is_none() || object.is_exact_instance_of::<PyInt>() || object.is_exact_instance_of::<PyFloat>()
            || object.is_exact_instance_of::<PyBool>() || object.is_exact_instance_of::<PyString>()
            || object.is_exact_instance_of::<PyList>() || object.is_exact_instance_of::<PyDict>()
            || object.is_exact_instance_of::<PyTuple>() { return Ok(PreparedAwait::Ready(value)); }
        let awaitable = unsafe { ffi::PyCoro_CheckExact(object.as_ptr()) != 0 }
            || generator_coroutine(object)?
            || object.is_instance(&py.import("collections.abc")?.getattr("Awaitable")?)?;
        if awaitable && adapter_probe {
            let adapter = py.import("docxtpl_native.bridge")?.getattr("_await_native_value")?.call1((object,))?;
            await_iterator(&adapter).map(PreparedAwait::Iterator)
        } else if awaitable { await_iterator(object).map(PreparedAwait::Iterator) }
        else { Ok(PreparedAwait::Ready(value)) }
    })
}

fn optional_method<'py>(value: &Bound<'py, PyAny>, name: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
    match value.getattr(name) {
        Ok(method) => Ok(Some(method)),
        Err(error) if error.is_instance_of::<PyAttributeError>(value.py()) => Ok(None),
        Err(error) => Err(error),
    }
}

fn close_unraisably(py: Python<'_>, iterator: &Bound<'_, PyAny>) {
    match optional_method(iterator, "close") {
        Ok(Some(close)) => if let Err(error) = close.call0() { error.write_unraisable(py, Some(iterator)); },
        Ok(None) => {},
        Err(error) => error.write_unraisable(py, Some(iterator)),
    }
}

enum AwaitStep {
    Complete(Py<PyAny>),
    Suspend(Py<PyAny>),
}

/// One send, throw or close of `iterator` (see `drive`). Never inlined: it
/// runs after a resume, possibly on another thread (see `fiber_local`).
#[inline(never)]
fn drive_step(iterator: &Py<PyAny>, input: Resume) -> PyResult<AwaitStep> {
    Python::attach(|py| -> PyResult<AwaitStep> {
        let iterator = iterator.bind(py);
        match input {
            Resume::Value(value) => {
                let mut output = std::ptr::null_mut();
                // All references are owned across the call; send dispatches
                // through CPython's iterator protocol, not instance lookup.
                let status = unsafe { ffi::PyIter_Send(iterator.as_ptr(), value.bind(py).as_ptr(), &mut output) };
                match status {
                    ffi::PySendResult::PYGEN_RETURN => Ok(AwaitStep::Complete(unsafe { Bound::from_owned_ptr_or_err(py, output)? }.unbind())),
                    ffi::PySendResult::PYGEN_NEXT => Ok(AwaitStep::Suspend(unsafe { Bound::from_owned_ptr_or_err(py, output)? }.unbind())),
                    ffi::PySendResult::PYGEN_ERROR => Err(PyErr::fetch(py)),
                }
            }
            Resume::Error(error) => {
                if error.is_instance_of::<PyGeneratorExit>(py) {
                    if let Some(close) = optional_method(iterator, "close")? { close.call0()?; }
                    return Err(error);
                }
                let Some(throw) = optional_method(iterator, "throw")? else { return Err(error); };
                match throw.call1((error.value(py),)) {
                    Ok(value) => Ok(AwaitStep::Suspend(value.unbind())),
                    Err(error) if error.is_instance_of::<PyStopIteration>(py) => Ok(AwaitStep::Complete(error.value(py).getattr("value")?.unbind())),
                    Err(error) => Err(error),
                }
            }
            Resume::Throw(args) => {
                let args = args.bind(py);
                let typ = args.get_item(0)?;
                if unsafe { ffi::PyErr_GivenExceptionMatches(typ.as_ptr(), py.get_type::<PyGeneratorExit>().as_ptr()) } != 0 {
                    #[cfg(Py_3_12)]
                    if let Err(error) = validate_throw(args) {
                        if let Some(driver) = current_driver() {
                            driver.deferred_cleanup.replace(Some(iterator.clone().unbind()));
                        }
                        return Err(error);
                    }
                    #[cfg(not(Py_3_12))]
                    if let Err(error) = validate_throw(args) {
                        close_unraisably(py, iterator);
                        return Err(error);
                    }
                    if let Some(close) = optional_method(iterator, "close")? { close.call0()?; }
                    return Err(match normalize_throw(args) { Ok(error) | Err(error) => error });
                }
                let Some(throw) = optional_method(iterator, "throw")? else {
                    return Err(match validate_throw(args) {
                        Ok(()) => match normalize_throw(args) { Ok(error) | Err(error) => error },
                        Err(error) => {
                            // A rejected throw unwinds the delegating
                            // coroutine, which closes its await iterator.
                            // Cleanup failures are unraisable and do not
                            // replace the original normalization error.
                            close_unraisably(py, iterator);
                            error
                        }
                    });
                };
                match throw.call(args, None) {
                    Ok(value) => Ok(AwaitStep::Suspend(value.unbind())),
                    Err(error) if error.is_instance_of::<PyStopIteration>(py) => Ok(AwaitStep::Complete(error.value(py).getattr("value")?.unbind())),
                    Err(error) => Err(error),
                }
            }
        }
    })
}

/// Drop a Python reference outside the caller's activation (see `drive_step`).
#[inline(never)]
pub(crate) fn release<T>(value: T) {
    drop(value);
}

#[inline(never)]
fn reentrant_suspension_error() -> PyErr {
    Python::attach(|py| crate::python_bridge::unsupported(py,
        "a reentrant loop comparison cannot suspend the active render"))
}

pub(crate) fn drive(iterator: Py<PyAny>) -> PyResult<Py<PyAny>> {
    struct Depth(Option<&'static Driver>);
    impl Drop for Depth {
        fn drop(&mut self) {
            if let Some(driver) = self.0 { driver.drive_depth.set(driver.drive_depth.get() - 1); }
        }
    }
    let depth = Depth(current_driver());
    if let Some(driver) = depth.0 { driver.drive_depth.set(driver.drive_depth.get() + 1); }
    // The iterator is released by `release`, outside this activation.
    struct Owned(Option<Py<PyAny>>);
    impl Drop for Owned {
        fn drop(&mut self) { release(self.0.take()); }
    }
    let iterator = Owned(Some(iterator));
    let mut input = Resume::Value(Python::attach(|py| py.None()));
    loop {
        let step = drive_step(iterator.0.as_ref().expect("owned until drive returns"), input)?;
        match step {
            AwaitStep::Complete(value) => return Ok(value),
            AwaitStep::Suspend(yielded) => {
                let Some(driver) = current_driver() else {
                    return Err(PyRuntimeError::new_err("native await outside an active render task"));
                };
                if reentrant_suspension_forbidden() { return Err(reentrant_suspension_error()); }
                // The driver is heap-owned by the RenderFiber and by its stack
                // closure; CURRENT is installed by every resume or unwind. The
                // yielder belongs to that live coroutine stack. The outermost
                // await, outside host calls and Python frames, has no pyo3
                // guard or Python frame live on the stack: it may resume on
                // another thread (see RenderFiber).
                driver.migratable.set(driver.drive_depth.get() == 1 && driver.pins.get() == 0
                    && crate::python_bridge::error_scope_depth() == driver.entry_scopes.get()
                    // SAFETY: the GIL is held; the frame pointer is only compared.
                    && unsafe { ffi::PyEval_GetFrame() } == driver.entry_frame.get());
                let pointer = driver.yielder.get();
                if pointer.is_null() {
                    input = Resume::Error(PyRuntimeError::new_err("native render task has not started"));
                    continue;
                }
                let yielder = unsafe { &*pointer };
                input = crate::python_macro::suspend(|| crate::python_deferred::suspend(
                    || minijinja::without_host_state(|| yielder.suspend(yielded))));
            }
        }
    }
}
