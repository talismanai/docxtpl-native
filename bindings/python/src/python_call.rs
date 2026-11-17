//! Callback dispatch for the native VM, preserving foreign object protocols.
use crate::python_format::optional_attribute;
use crate::pyglobals::{FrameGlobals, frame_global, handle_exception};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

/// Check that a captured Context.call is still the standard dispatcher.
pub(crate) fn standard_context_call(method: &Bound<'_, PyAny>) -> PyResult<bool> {
    let binding = crate::python_function::binding(method.py())?;
    if !method.get_type().is(&binding.getattr("_METHOD_TYPE")?) { return Ok(false); }
    crate::python_function::is_standard_function(&method.getattr("__func__")?,
        &binding.getattr("_CONTEXT_CALL")?, &binding.getattr("_CONTEXT_CALL_CODE")?,
        binding.getattr("_RUNTIME_MODULE")?.cast()?, "call")
}

/// Apply the standard Context.call exception handler to a native callback.
pub(crate) fn handle_standard_context_error<'py>(method: &Bound<'py, PyAny>, error: PyErr)
    -> PyResult<Bound<'py, PyAny>> {
    let function = method.getattr("__func__")?;
    let scope = FrameGlobals::of_function(&function)?;
    let context = method.getattr("__self__")?;
    handle_exception(&scope, error, &["StopIteration"], || context
        .getattr("environment")?
        .getattr("undefined")?
        .call1(("value was undefined because a callable raised a StopIteration exception",)))
}

pub(crate) fn pass_arg<'py>(method: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    let binding = py.import("docxtpl_native.bridge")?;
    let standard = if method.get_type().is(&binding.getattr(pyo3::intern!(py, "_METHOD_TYPE"))?) {
        crate::python_function::is_standard_function(
            &method.getattr(pyo3::intern!(py, "__func__"))?,
            &binding.getattr(pyo3::intern!(py, "_PASS_ARG_FROM_OBJ"))?,
            &binding.getattr(pyo3::intern!(py, "_PASS_ARG_FROM_OBJ_CODE"))?,
            binding.getattr(pyo3::intern!(py, "_UTILS_MODULE"))?.cast()?,
            "from_obj",
        )?
    } else { false };
    if !standard {
        return method.call1((value,));
    }
    let scope = FrameGlobals::of_function(&method.getattr(pyo3::intern!(py, "__func__"))?)?;
    // _PassArg.from_obj checks presence, then reads again. Descriptors can
    // observe both accesses; a single getattr-with-default would change them.
    if frame_global(&scope, pyo3::intern!(py, "hasattr"))?
        .call1((value, pyo3::intern!(py, "jinja_pass_arg")))?.is_truthy()? {
        value.getattr(pyo3::intern!(py, "jinja_pass_arg"))
    } else {
        Ok(py.None().into_bound(py))
    }
}

/// Bind **kwargs for a standard variadic method whose fixed arguments have
/// already been supplied positionally. Reuse CPython's equality, dict and
/// Unicode protocols; no second general Python argument parser is needed.
/// Matching order follows CPython 3.11/3.12 initialize_locals (PSF license,
/// vendor/notices/CPython-LICENSE): identity scan, equality scan, dict insertion.
pub(crate) fn bound_keywords<'py>(
    function: &Bound<'py, PyAny>,
    kwargs: &Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyDict>> {
    let py = function.py();
    let result = PyDict::new(py);
    if kwargs.is_empty() { return Ok(result); }
    let code = function.getattr(pyo3::intern!(py, "__code__"))?;
    let names = code.getattr(pyo3::intern!(py, "co_varnames"))?.cast_into::<PyTuple>()?;
    let count = code.getattr(pyo3::intern!(py, "co_argcount"))?.extract::<usize>()?;
    // Vectorcall snapshots keyword names/values before any comparison. This
    // also keeps mutations during __eq__ from changing the arguments in flight.
    let pairs = crate::python_keywords::snapshot(kwargs)?;
    let parameters = names.iter().take(count).collect::<Vec<_>>();
    for pair in pairs.iter() {
        let key = pair.get_item(0)?;
        if crate::python_keywords::parameter_index(&key, &parameters)?.is_some() {
            return Err(crate::python_keywords::argument_error(function, &key,
                "() got multiple values for argument '")?);
        }
        result.set_item(key, pair.get_item(1)?)?;
    }
    Ok(result)
}

pub(crate) fn arguments<'py>(
    py: Python<'py>,
    mut prefix: Vec<Bound<'py, PyAny>>,
    args: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    for value in args.try_iter()? {
        prefix.push(value?);
    }
    PyTuple::new(py, prefix)
}

fn context_call<'py>(
    py: Python<'py>,
    scope: &FrameGlobals<'py>,
    context: &Bound<'py, PyAny>,
    callback: &Bound<'py, PyAny>,
    args: &Bound<'py, PyTuple>,
    kwargs: &Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyAny>> {
    let mut callback = callback.clone();
    if frame_global(scope, pyo3::intern!(py, "hasattr"))?
        .call1((&callback, pyo3::intern!(py, "__call__")))?.is_truthy()? {
        // Resolve the method before evaluating its argument: a descriptor may
        // replace _PassArg or from_obj while __call__ is being read.
        let method = frame_global(scope, pyo3::intern!(py, "_PassArg"))?.getattr("from_obj")?;
        if !pass_arg(&method, &callback.getattr("__call__")?)?.is_none() {
            callback = callback.getattr("__call__")?;
        }
    }
    let method = frame_global(scope, pyo3::intern!(py, "_PassArg"))?.getattr("from_obj")?;
    let kind = pass_arg(&method, &callback)?;
    let mut context = context.clone();
    let mut prefix = Vec::new();
    if kind.is(&frame_global(scope, pyo3::intern!(py, "_PassArg"))?.getattr("context")?) {
        for name in ["_loop_vars", "_block_vars"] {
            if kwargs.call_method1("get", (name,))?.is_truthy()? {
                let locals = kwargs.as_any().get_item(name)?;
                context = crate::python_context_helpers::python_context_derived(py, &context, &locals)?;
            }
        }
        prefix.push(context.clone());
    } else if kind.is(&frame_global(scope, pyo3::intern!(py, "_PassArg"))?.getattr("eval_context")?) {
        prefix.push(context.getattr("eval_ctx")?);
    } else if kind.is(&frame_global(scope, pyo3::intern!(py, "_PassArg"))?.getattr("environment")?) {
        prefix.push(context.getattr("environment")?);
    }
    for name in ["_block_vars", "_loop_vars"] {
        kwargs.call_method1("pop", (name, py.None()))?;
    }
    let arguments = if prefix.is_empty() {
        args.clone()
    } else {
        arguments(py, prefix, args.as_any())?
    };
    match crate::python_macro::dispatch(&callback, &arguments, kwargs) {
        Ok(value) => Ok(value),
        Err(error) => handle_exception(scope, error, &["StopIteration"], || context
            .getattr("environment")?
            .getattr("undefined")?
            .call1(("value was undefined because a callable raised a StopIteration exception",))),
    }
}

pub(crate) fn call_context_method<'py>(
    py: Python<'py>,
    context: &Bound<'py, PyAny>,
    callback: &Bound<'py, PyAny>,
    args: &Bound<'py, PyAny>,
    kwargs: &Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyAny>> {
    call_context_keywords(py, context, callback, args, kwargs, None, None)
}

fn call_context_keywords<'py>(
    py: Python<'py>, context: &Bound<'py, PyAny>, callback: &Bound<'py, PyAny>,
    args: &Bound<'py, PyAny>, kwargs: &Bound<'py, PyDict>, sources: Option<&Bound<'py, PyList>>,
    prepared: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let method = match prepared { Some(value) => value.clone(), None => context.getattr("call")? };
    let merged = sources.map(|sources| crate::python_keywords::merge(&method, sources)).transpose()?;
    let kwargs = merged.as_ref().unwrap_or(kwargs);
    let original = py
        .import("docxtpl_native.bridge")?
        .getattr("_CONTEXT_CALL")?;
    let original_code = py
        .import("docxtpl_native.bridge")?
        .getattr("_CONTEXT_CALL_CODE")?;
    let method_type = py.import("types")?.getattr("MethodType")?;
    let standard = if method.get_type().is(&method_type) {
        let function = method.getattr("__func__")?;
        crate::python_function::is_standard_function(
            &function, &original, &original_code,
            py.import("docxtpl_native.bridge")?.getattr("_RUNTIME_MODULE")?.cast()?, "call")?
    } else {
        false
    };
    if standard {
        let args = arguments(py, Vec::new(), args)?;
        let function = method.getattr("__func__")?;
        let scope = FrameGlobals::of_function(&function)?;
        let kwargs = bound_keywords(&function, kwargs)?;
        return context_call(py, &scope, &method.getattr("__self__")?, callback, &args, &kwargs);
    }
    // A user override remains a callback, rather than being bypassed by
    // the standard native algorithm.
    method.call(arguments(py, vec![callback.clone()], args)?, Some(kwargs))
}

#[pyfunction]
#[pyo3(signature = (config, callback, args, kwargs, visible, autoescape, trusted=false))]
pub(crate) fn python_invoke<'py>(
    py: Python<'py>,
    config: &Bound<'py, PyAny>,
    callback: &Bound<'py, PyAny>,
    args: &Bound<'py, PyAny>,
    kwargs: &Bound<'py, PyDict>,
    visible: &Bound<'py, PyAny>,
    autoescape: bool,
    trusted: bool,
) -> PyResult<Bound<'py, PyAny>> {
    invoke_keywords(py, config, callback, args, kwargs, visible, autoescape, trusted, None, None, None, None)
}

/// Whether `callback` is one of the default environment's filters or tests
/// as the bridge module captured them (identity, and code for functions).
fn standard_callback(py: Python<'_>, callback: &Bound<'_, PyAny>) -> PyResult<bool> {
    let table = crate::python_function::binding(py)?.getattr(pyo3::intern!(py, "_STANDARD_CALLBACKS"))?;
    let Some(entry) = table.cast::<PyDict>()?.get_item(callback.as_ptr() as usize)? else { return Ok(false); };
    let (original, code) = entry.extract::<(Bound<'_, PyAny>, Bound<'_, PyAny>)>()?;
    if !original.is(callback) { return Ok(false); }
    Ok(code.is_none() || callback.getattr(pyo3::intern!(py, "__code__"))?.is(&code))
}

pub(crate) fn invoke_keywords<'py>(
    py: Python<'py>, config: &Bound<'py, PyAny>, callback: &Bound<'py, PyAny>,
    args: &Bound<'py, PyAny>, kwargs: &Bound<'py, PyDict>, visible: &Bound<'py, PyAny>,
    autoescape: bool, trusted: bool, sources: Option<&Bound<'py, PyList>>,
    runtime_context: Option<&Bound<'py, PyAny>>,
    call_policy: Option<bool>,
    prepared: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    // A callback may run user code that changes what later sources compile
    // against; Jinja's own filters and tests do not.
    if !standard_callback(py, callback)? {
        config.setattr("_simple_names", py.None())?;
    }
    let environment = config.getattr("environment")?;
    if !trusted {
        let context = match runtime_context { Some(context) => context.clone(),
            None => crate::python_context::python_context(py, config, visible, autoescape)? };
        let sandboxed = match call_policy { Some(value) => value, None => environment.getattr("sandboxed")?.is_truthy()? };
        if sandboxed {
            return crate::python_sandbox::call_keywords(
                py, &environment, &context, callback, args, kwargs, sources, prepared,
            );
        }
        return call_context_keywords(py, &context, callback, args, kwargs, sources, prepared);
    }
    let kind = optional_attribute(&optional_attribute(callback, "jinja_pass_arg")?, "name")?;
    if kind.eq("context")? {
        args.call_method1(
            "insert",
            (0, match runtime_context { Some(context) => context.clone(),
                None => crate::python_context::python_context(py, config, visible, autoescape)? }),
        )?;
    } else if kind.eq("eval_context")? {
        let context = match runtime_context { Some(context) => context.getattr("eval_ctx")?, None => {
            let context = crate::python_context::python_eval_context(py, &environment)?;
            context.setattr("autoescape", autoescape)?;
            context
        }};
        args.call_method1("insert", (0, context))?;
    } else if kind.eq("environment")? {
        args.call_method1("insert", (0, environment))?;
    }
    let merged = sources.map(|sources| crate::python_keywords::merge(callback, sources)).transpose()?;
    crate::python_macro::dispatch(callback, &arguments(py, Vec::new(), args)?, merged.as_ref().unwrap_or(kwargs))
}
