//! Foreign macro argument binding; bodies execute in the existing Rust VM.
use crate::pyglobals::{FrameGlobals, frame_global, handle_exception};
use crate::python_bridge::Bridge;
use minijinja::{Error, State, Value};
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PySlice, PyTuple};
use std::sync::{Arc, Weak};

// Run a macro body in the active state, or (`detach`) prepare it to run on
// a detached copy of that state.
type Body<'a> = &'a dyn Fn(&Arc<minijinja::RuntimeMacro>, Vec<Py<PyAny>>, bool) -> PyResult<BodyRun>;

enum BodyRun {
    Done(Py<PyAny>),
    Detached(Box<DetachedMacro>),
    NotDetachable,
}

/// A macro body bound to a detached copy of its defining activation.
struct DetachedMacro {
    host: minijinja::DetachedHostState,
    implementation: Arc<minijinja::RuntimeMacro>,
    values: Vec<Value>,
    bridge: Arc<Bridge>,
}

impl DetachedMacro {
    fn run(self) -> PyResult<Py<PyAny>> {
        let DetachedMacro { host, implementation, values, bridge } = self;
        let value = host.run(|state| {
            let frames = state.render_frames();
            let filtered_loop_depth = state.filtered_loop_depth();
            let state = std::cell::RefCell::new(state);
            crate::python_async::with_render_frames(frames, filtered_loop_depth, || {
                with_state_cell(&bridge, &state, || {
                    let state = state.try_borrow().map_err(|_| minijinja::Error::new(
                        minijinja::ErrorKind::InvalidOperation, "render state is already active"))?;
                    implementation.call_bound(&state, values)
                })
            })
        }).map_err(|error| bridge.take_error().unwrap_or_else(|| crate::error(error)))?;
        // After the body, possibly on another thread (see fiber_local).
        crate::fiber_local::attach(|py| bridge.python(py, &value))
    }
}
// The active VM call's body runner: a pointer to a `Body` on the stack of
// `with_state`/`with_state_cell`, valid while their scope runs; null when
// there is none.
crate::fiber_local::fiber_slot!(static BODY);

// A suspended coroutine must not leave a borrowed stack callback in TLS.
pub(crate) fn suspend<R>(f: impl FnOnce() -> R) -> R {
    BODY.set(std::ptr::null(), f)
}

fn with_body<R>(body: Body<'_>, f: impl FnOnce() -> R) -> R {
    BODY.set(&raw const body as *const (), f)
}

#[pyclass]
struct MacroBody {
    implementation: Weak<minijinja::RuntimeMacro>,
    asynchronous: bool,
}

fn execute_body(
    implementation: &Weak<minijinja::RuntimeMacro>,
    args: &Bound<'_, PyTuple>,
) -> PyResult<Py<PyAny>> {
    match run_body(implementation, args, false)? {
        BodyRun::Done(value) => Ok(value),
        BodyRun::Detached(_) | BodyRun::NotDetachable => unreachable!("the body ran in place"),
    }
}

fn run_body(
    implementation: &Weak<minijinja::RuntimeMacro>,
    args: &Bound<'_, PyTuple>,
    detach: bool,
) -> PyResult<BodyRun> {
    let pointer = BODY.get().cast::<Body<'static>>();
    if !pointer.is_null() {
        // SAFETY: `with_body` installed a pointer to a `Body` that lives for
        // its scope, which encloses this call; it does not escape here.
        let body = unsafe { &*pointer };
        return match implementation.upgrade() {
            Some(implementation) => {
                body(&implementation, args.iter().map(Bound::unbind).collect(), detach)
            }
            None => Err(crate::python_bridge::unsupported(
                args.py(),
                "macro body after its defining render ended",
            )),
        };
    }
    Err(crate::python_bridge::unsupported(
        args.py(),
        "macro body outside its active VM call",
    ))
}

#[pyclass]
struct MacroBodyAwaitable {
    implementation: Weak<minijinja::RuntimeMacro>,
    arguments: Option<Py<PyTuple>>,
}

#[pymethods]
impl MacroBodyAwaitable {
    fn __await__(slf: Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        // Awaited where the running render cannot suspend (inside a host
        // callback that borrows its state), the body runs on a detached copy
        // of the activation in its own coroutine, so it can suspend alone.
        let py = slf.py();
        if crate::python_async::reentrant_suspension_forbidden() {
            let arguments = slf.borrow().arguments.as_ref().map(|arguments| arguments.clone_ref(py));
            if let Some(arguments) = arguments {
                let implementation = slf.borrow().implementation.clone();
                if let BodyRun::Detached(detached) = run_body(&implementation, arguments.bind(py), true)? {
                    slf.borrow_mut().arguments = None;
                    let bridge = detached.bridge.clone();
                    return crate::python_async::native_coroutine(py, bridge, move || detached.run());
                }
            }
        }
        Ok(slf.into_any().unbind())
    }
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }
    fn __next__(&mut self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        // Template code under this host call keeps the fiber on its thread.
        crate::python_async::pinned(move || -> PyResult<Py<PyAny>> {
        let Some(arguments) = self.arguments.take() else {
            return Err(pyo3::exceptions::PyStopIteration::new_err(()));
        };
        let result = execute_body(&self.implementation, arguments.bind(py)).map_err(|error| {
            crate::python_errors::escaped_iteration(
                py,
                error,
                crate::python_errors::Boundary::Coroutine,
            )
        })?;
        Err(pyo3::exceptions::PyStopIteration::new_err((result,)))
            })
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(arguments) = &self.arguments {
            visit.call(arguments)?;
        }
        Ok(())
    }
    fn __clear__(&mut self) {
        self.arguments = None;
    }
}

#[pyclass]
struct MacroAwaitable {
    frame: Option<(Py<PyAny>, Py<PyAny>, Py<PyAny>, Py<PyAny>)>,
}

#[pymethods]
impl MacroAwaitable {
    fn __await__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }
    fn __next__(&mut self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        // Template code under this host call keeps the fiber on its thread.
        crate::python_async::pinned(move || -> PyResult<Py<PyAny>> {
        let Some((object, arguments, autoescape, function)) = self.frame.take() else {
            return Err(pyo3::exceptions::PyStopIteration::new_err(()));
        };
        let result = (|| -> PyResult<Py<PyAny>> {
            let scope = FrameGlobals::of_function(function.bind(py))?;
            let callback = object.bind(py).getattr("_func")?;
            let result = callback.call(
                PyTuple::new(
                    py,
                    arguments
                        .bind(py)
                        .try_iter()?
                        .collect::<PyResult<Vec<_>>>()?,
                )?,
                None,
            )?;
            let result = crate::python_async::drive(crate::python_async::await_iterator(&result)?)?
                .into_bound(py);
            let result = if autoescape.bind(py).is_truthy()? {
                frame_global(&scope, pyo3::intern!(py, "Markup"))?.call1((result,))?
            } else {
                result
            };
            Ok(result.unbind())
        })()
        .map_err(|error| {
            crate::python_errors::escaped_iteration(
                py,
                error,
                crate::python_errors::Boundary::Coroutine,
            )
        })?;
        Err(pyo3::exceptions::PyStopIteration::new_err((result,)))
            })
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some((object, arguments, autoescape, function)) = &self.frame {
            visit.call(object)?;
            visit.call(arguments)?;
            visit.call(autoescape)?;
            visit.call(function)?;
        }
        Ok(())
    }
    fn __clear__(&mut self) {
        self.frame = None;
    }
}

#[pymethods]
impl MacroBody {
    #[pyo3(signature = (*args))]
    fn __call__(&self, args: &Bound<'_, PyTuple>) -> PyResult<Py<PyAny>> {
        // Template code under this host call keeps the fiber on its thread.
        crate::python_async::pinned(move || -> PyResult<Py<PyAny>> {
        if self.asynchronous {
            let py = args.py();
            let awaitable = Py::new(
                py,
                MacroBodyAwaitable {
                    implementation: self.implementation.clone(),
                    arguments: Some(args.clone().unbind()),
                },
            )?;
            return Ok(crate::python_function::binding(py)?
                .getattr("_await_native_render")?
                .call1((awaitable,))?
                .unbind());
        }
        execute_body(&self.implementation, args)
            })
    }
}

fn standard_method(method: &Bound<'_, PyAny>, key: &str, name: &str) -> PyResult<bool> {
    let binding = crate::python_function::binding(method.py())?;
    Ok(method.get_type().is(&binding.getattr("_METHOD_TYPE")?)
        && crate::python_function::is_standard_function(
            &method.getattr("__func__")?,
            &binding.getattr(key)?,
            &binding.getattr(format!("{key}_CODE"))?,
            binding.getattr("_RUNTIME_MODULE")?.cast()?,
            name,
        )?)
}

fn create<'py>(
    py: Python<'py>,
    bridge: &Arc<Bridge>,
    state: &State,
    value: &Value,
    function: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let binding = crate::python_function::binding(py)?;
    let class = binding.getattr("_RUNTIME_MODULE")?.getattr("Macro")?;
    let environment = bridge.config.bind(py).getattr("environment")?;
    let get = |key: &str| bridge.python(py, &value.get_attr(key).map_err(crate::error)?);
    let name = get("name")?;
    let arguments = get("arguments")?;
    let kwargs = get("catch_kwargs")?;
    let varargs = get("catch_varargs")?;
    let caller = get("caller")?;
    let context = bridge.python(py, state.runtime_context())?;
    let autoescape = context
        .bind(py)
        .getattr("eval_ctx")?
        .getattr("autoescape")?;
    let Some(object) =
        crate::python_context::standard_instance(py, &class, crate::python_context::Kind::Macro)?
    else {
        return class.call1((
            environment,
            function,
            name,
            arguments,
            kwargs,
            varargs,
            caller,
            autoescape,
        ));
    };
    let scope = FrameGlobals::of_function(&binding.getattr("_MACRO_INIT")?)?;
    object.setattr("_environment", &environment)?;
    object.setattr("_func", function)?;
    object.setattr(
        "_argument_count",
        frame_global(&scope, pyo3::intern!(py, "len"))?.call1((arguments.bind(py),))?,
    )?;
    object.setattr("name", name)?;
    object.setattr("arguments", arguments.bind(py))?;
    object.setattr("catch_kwargs", kwargs)?;
    object.setattr("catch_varargs", varargs)?;
    object.setattr("caller", caller)?;
    object.setattr("explicit_caller", arguments.bind(py).contains("caller")?)?;
    let autoescape = if autoescape.is_none() {
        if frame_global(&scope, pyo3::intern!(py, "callable"))?
            .call1((environment.getattr("autoescape")?,))?
            .is_truthy()?
        {
            environment.getattr("autoescape")?.call1((py.None(),))?
        } else {
            environment.getattr("autoescape")?
        }
    } else {
        autoescape
    };
    object.setattr("_default_autoescape", autoescape)?;
    Ok(object)
}

pub(crate) fn dispatch<'py>(
    callback: &Bound<'py, PyAny>,
    args: &Bound<'py, PyTuple>,
    kwargs: &Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyAny>> {
    if let Some(value) = crate::native_join::try_join(callback, args, kwargs)? {
        return Ok(value);
    }
    if let Some(value) = crate::native_filters::try_call(callback, args, kwargs)? {
        return Ok(value);
    }
    if let Some(value) = crate::native_tests::try_call(callback, args, kwargs)? {
        return Ok(value);
    }
    if !standard_method(callback, "_MACRO_CALL", "__call__")? {
        return callback.call(args, Some(kwargs));
    }
    let py = callback.py();
    let object = callback.getattr("__self__")?;
    let function = callback.getattr("__func__")?;
    let kwargs = crate::python_call::bound_keywords(&function, kwargs)?;
    let scope = FrameGlobals::of_function(&function)?;
    let (args, autoescape) = if !args.is_empty()
        && frame_global(&scope, pyo3::intern!(py, "isinstance"))?
            .call1((
                args.get_item(0)?,
                frame_global(&scope, pyo3::intern!(py, "EvalContext"))?,
            ))?
            .is_truthy()?
    {
        (
            args.get_slice(1, args.len()),
            args.get_item(0)?.getattr("autoescape")?,
        )
    } else {
        (args.clone(), object.getattr("_default_autoescape")?)
    };
    let count = object.getattr("_argument_count")?;
    let slice = PySlice::new(py, 0, count.extract::<isize>()?, 1);
    let arguments = frame_global(&scope, pyo3::intern!(py, "list"))?
        .call1((args.as_any().get_item(slice)?,))?;
    let off = frame_global(&scope, pyo3::intern!(py, "len"))?.call1((&arguments,))?;
    let mut found_caller = false;
    if !off.eq(&count)? {
        let start = frame_global(&scope, pyo3::intern!(py, "len"))?
            .call1((&arguments,))?
            .extract::<isize>()?;
        let names =
            object
                .getattr("arguments")?
                .get_item(PySlice::new(py, start, isize::MAX, 1))?;
        for name in names.try_iter()? {
            let name = name?;
            let value = match kwargs.call_method1("pop", (&name,)) {
                Ok(value) => value,
                Err(error) => handle_exception(&scope, error, &["KeyError"], || {
                    frame_global(&scope, pyo3::intern!(py, "missing"))
                })?,
            };
            if name.eq("caller")? {
                found_caller = true;
            }
            arguments.call_method1("append", (value,))?;
        }
    } else {
        found_caller = object.getattr("explicit_caller")?.is_truthy()?;
    }
    if object.getattr("caller")?.is_truthy()? && !found_caller {
        let caller = kwargs.call_method1("pop", ("caller", py.None()))?;
        let caller = if caller.is_none() {
            let keywords = PyDict::new(py);
            keywords.set_item("name", "caller")?;
            object
                .getattr("_environment")?
                .getattr("undefined")?
                .call(("No caller defined",), Some(&keywords))?
        } else {
            caller
        };
        arguments.call_method1("append", (caller,))?;
    }
    if object.getattr("catch_kwargs")?.is_truthy()? {
        arguments.call_method1("append", (&kwargs,))?;
    } else if !kwargs.is_empty() {
        let name = object.getattr("name")?.repr()?;
        let message = if kwargs.contains("caller")? {
            format!(
                "macro {name} was invoked with two values for the special caller argument. This is most likely a bug."
            )
        } else {
            let key = frame_global(&scope, pyo3::intern!(py, "next"))?
                .call1((frame_global(&scope, pyo3::intern!(py, "iter"))?.call1((&kwargs,))?,))?
                .repr()?;
            format!("macro {name} takes no keyword argument {key}")
        };
        return Err(PyErr::from_value(
            frame_global(&scope, pyo3::intern!(py, "TypeError"))?.call1((message,))?,
        ));
    }
    if object.getattr("catch_varargs")?.is_truthy()? {
        arguments.call_method1(
            "append",
            (args
                .as_any()
                .get_item(PySlice::new(py, count.extract()?, isize::MAX, 1))?,),
        )?;
    } else if frame_global(&scope, pyo3::intern!(py, "len"))?
        .call1((&args,))?
        .gt(&count)?
    {
        let message = format!(
            "macro {} takes not more than {} argument(s)",
            object.getattr("name")?.repr()?,
            frame_global(&scope, pyo3::intern!(py, "len"))?
                .call1((object.getattr("arguments")?,))?
        );
        return Err(PyErr::from_value(
            frame_global(&scope, pyo3::intern!(py, "TypeError"))?.call1((message,))?,
        ));
    }
    let invoke = object.getattr("_invoke")?;
    if !standard_method(&invoke, "_MACRO_INVOKE", "_invoke")? {
        return invoke.call1((arguments, autoescape));
    }
    let object = invoke.getattr("__self__")?;
    if object
        .getattr("_environment")?
        .getattr("is_async")?
        .is_truthy()?
    {
        let method = object.getattr("_async_invoke")?;
        if !standard_method(&method, "_MACRO_ASYNC_INVOKE", "_async_invoke")? {
            return method.call1((arguments, autoescape));
        }
        let awaitable = Py::new(
            py,
            MacroAwaitable {
                frame: Some((
                    method.getattr("__self__")?.unbind(),
                    arguments.unbind(),
                    autoescape.unbind(),
                    method.getattr("__func__")?.unbind(),
                )),
            },
        )?;
        return crate::python_function::binding(py)?
            .getattr("_await_native_render")?
            .call1((awaitable,));
    }
    let scope = FrameGlobals::of_function(&invoke.getattr("__func__")?)?;
    let result = object.getattr("_func")?.call(
        PyTuple::new(py, arguments.try_iter()?.collect::<PyResult<Vec<_>>>()?)?,
        None,
    )?;
    if autoescape.is_truthy()? {
        frame_global(&scope, pyo3::intern!(py, "Markup"))?.call1((result,))
    } else {
        Ok(result)
    }
}

pub(crate) fn with_state<R>(bridge: &Arc<Bridge>, state: &State, f: impl FnOnce() -> R) -> R {
    let execute = |implementation: &Arc<minijinja::RuntimeMacro>, args: Vec<Py<PyAny>>, detach: bool| {
        execute_in_state(bridge, state, implementation, args, detach)
    };
    with_body(&execute, f)
}

pub(crate) fn with_state_cell<R>(bridge: &Arc<Bridge>, state: &std::cell::RefCell<&mut State>,
    f: impl FnOnce() -> R) -> R {
    let execute = |implementation: &Arc<minijinja::RuntimeMacro>, args: Vec<Py<PyAny>>, detach: bool| {
        let state = state.try_borrow().map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("render state is already active"))?;
        execute_in_state(bridge, &state, implementation, args, detach)
    };
    with_body(&execute, f)
}

fn execute_in_state(bridge: &Arc<Bridge>, state: &State,
    implementation: &Arc<minijinja::RuntimeMacro>, args: Vec<Py<PyAny>>, detach: bool) -> PyResult<BodyRun> {
    if !implementation.is_available_in(state) {
        return Python::attach(|py| Err(crate::python_bridge::unsupported(py,
            "macro escaped its defining VM scope; retained macro execution is not implemented")));
    }
    let values = Python::attach(|py| {
        let missing = crate::python_function::binding(py)?
            .getattr("_RUNTIME_MODULE")?
            .getattr("missing")?;
        args.iter()
            .map(|value| {
                if value.bind(py).is(&missing) {
                    Ok(Value::UNDEFINED)
                } else if let Some(value) = native_value(value.bind(py))? {
                    Ok(value)
                } else {
                    Ok(bridge.opaque_value(value.bind(py)))
                }
            })
            .collect::<PyResult<Vec<_>>>()
    })?;
    if detach {
        // SAFETY: every bridge environment is an Environment<'static> (configure).
        return Ok(match unsafe { state.detach_for_host() } {
            Some(host) => BodyRun::Detached(Box::new(DetachedMacro { host, implementation: implementation.clone(), values,
                bridge: bridge.clone() })),
            None => BodyRun::NotDetachable,
        });
    }
    let result = implementation
        .call_bound(state, values)
        .map_err(|error| bridge.take_error().unwrap_or_else(|| crate::error(error)))?;
    Python::attach(|py| bridge.python(py, &result)).map(BodyRun::Done)
}

pub(crate) fn native_value(value: &Bound<'_, PyAny>) -> PyResult<Option<Value>> {
    let binding = crate::python_function::binding(value.py())?;
    if value.get_type().is(&binding.getattr("_MACRO_TYPE")?) {
        if let Ok(function) = value.getattr("_func")?.cast::<MacroBody>() {
            return Ok(function
                .borrow()
                .implementation
                .upgrade()
                .map(Value::from_dyn_object));
        }
    }
    Ok(None)
}

pub(crate) fn create_value(
    bridge: &Arc<Bridge>,
    state: &State,
    value: &Value,
) -> Result<Value, Error> {
    Python::attach(|py| {
        let implementation = value
            .downcast_object::<minijinja::RuntimeMacro>()
            .ok_or_else(|| {
                crate::python_bridge::unsupported(py, "missing native macro implementation")
            })?;
        let endpoint = Py::new(
            py,
            MacroBody {
                implementation: Arc::downgrade(&implementation),
                asynchronous: bridge.asynchronous,
            },
        )?;
        let callable = create(py, bridge, state, value, endpoint.bind(py).as_any())?;
        Ok(bridge.opaque_value(&callable))
    })
    .map_err(|error| bridge.remember(error))
}

pub(crate) fn call(
    bridge: &Arc<Bridge>,
    state: &State,
    value: &Value,
    args: &[Value],
) -> Result<Value, Error> {
    Python::attach(|py| {
        let callable = bridge.python(py, value)?;
        bridge.invoke(callable.bind(py), state, args, false)
    })
    .map_err(|error| bridge.remember(error))
}
