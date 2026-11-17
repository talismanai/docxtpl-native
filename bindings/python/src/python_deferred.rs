//! Continuation for an asynchronously awaited native block, `super()` or
//! recursive loop call. Awaited inside the render that created it, the body
//! runs on that render's state. Awaited after that render ended, or from
//! another task while it is suspended, it runs on a detached copy of the
//! activation (`minijinja::DetachedActivation`) in its own native coroutine.
use std::cell::RefCell;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use pyo3::exceptions::{PyRuntimeError, PyStopIteration};
use pyo3::prelude::*;

use crate::python_bridge::Bridge;

type Body<'a> = &'a dyn Fn() -> PyResult<Py<PyAny>>;
// The active deferred body: a pointer to a `(token, Body)` on the stack of
// `with_body`, valid while its scope runs; null when there is none.
crate::fiber_local::fiber_slot!(static BODY);
static NEXT_TOKEN: AtomicU64 = AtomicU64::new(1);

pub(crate) fn suspend<R>(f: impl FnOnce() -> R) -> R {
    BODY.set(std::ptr::null(), f)
}

/// The active deferred body, if any.
fn current() -> Option<&'static (u64, Body<'static>)> {
    let pointer = BODY.get().cast::<(u64, Body<'static>)>();
    // SAFETY: `with_body` installed a pointer to a pair that lives for its
    // scope, which encloses every caller; the reference does not escape them.
    (!pointer.is_null()).then(|| unsafe { &*pointer })
}

pub(crate) fn with_body<R>(body: &mut dyn FnMut() -> PyResult<Py<PyAny>>,
    run: impl FnOnce(u64) -> R) -> R {
    let token = NEXT_TOKEN.fetch_add(1, Ordering::Relaxed);
    let body = RefCell::new(body);
    let invoke = || {
        let mut body = body.try_borrow_mut().map_err(|_| PyRuntimeError::new_err("coroutine already executing"))?;
        body()
    };
    let current: (u64, Body<'_>) = (token, &invoke);
    BODY.set(&raw const current as *const (), || run(token))
}

fn active(token: u64) -> bool {
    current().is_some_and(|(current, _)| *current == token)
}

/// The detached copy of the activation and the bridge that converts its result.
pub(crate) struct Detached {
    pub(crate) activation: minijinja::DetachedActivation,
    pub(crate) bridge: Arc<Bridge>,
}

impl Detached {
    fn run(self) -> PyResult<Py<PyAny>> {
        let Detached { activation, bridge } = self;
        let value = activation.run(|state, execute| {
            let frames = state.render_frames();
            let filtered_loop_depth = state.filtered_loop_depth();
            let state = RefCell::new(state);
            crate::python_async::with_render_frames(frames, filtered_loop_depth, || {
                crate::python_macro::with_state_cell(&bridge, &state, || {
                    let mut state = state.try_borrow_mut().map_err(|_| minijinja::Error::new(
                        minijinja::ErrorKind::InvalidOperation, "render state is already active"))?;
                    execute(&mut state)
                })
            })
        }).map_err(|error| bridge.take_error().unwrap_or_else(|| crate::error(error)))?;
        Python::attach(|py| bridge.python(py, &value))
    }
}

#[pyclass(module = "docxtpl_native._native")]
struct DeferredBody {
    token: u64,
    consumed: bool,
    detached: Mutex<Option<Detached>>,
}

#[pymethods]
impl DeferredBody {
    fn __await__(slf: Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let detached = {
            let body = slf.borrow();
            if body.consumed || active(body.token) { None }
            else { body.detached.lock().unwrap().take() }
        };
        match detached {
            Some(detached) => {
                slf.borrow_mut().consumed = true;
                let bridge = detached.bridge.clone();
                crate::python_async::native_coroutine(py, bridge, move || detached.run())
            }
            None => Ok(slf.into_any().unbind()),
        }
    }
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }

    fn __next__(&mut self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        // Template code under this host call keeps the fiber on its thread.
        crate::python_async::pinned(move || -> PyResult<Py<PyAny>> {
        if self.consumed { return Err(PyRuntimeError::new_err("cannot reuse already awaited coroutine")); }
        self.consumed = true;
        let result = match current() {
            Some((token, body)) if *token == self.token => body(),
            _ => Err(crate::python_bridge::unsupported(py, "deferred block outside its active render")),
        }?;
        Err(PyStopIteration::new_err((result,)))
            })
    }
}

pub(crate) fn coroutine(py: Python<'_>, token: u64, detached: Option<Detached>) -> PyResult<Py<PyAny>> {
    let body = Py::new(py, DeferredBody { token, consumed: false, detached: Mutex::new(detached) })?;
    Ok(crate::python_function::binding(py)?.getattr("_await_native_render")?.call1((body,))?.unbind())
}
