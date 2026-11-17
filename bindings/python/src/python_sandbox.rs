//! Standard sandbox call policies; user overrides remain foreign callbacks.
use crate::pyglobals::{frame_global, FrameGlobals};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple};

/// Bypass the standard format-method check only for exact builtin scalars
/// when its global dependencies have their ordinary, non-observable form.
/// All other values and replaced callbacks keep their original call path.
pub(crate) fn wrap_str_format<'py>(method: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    use pyo3::types::{PyBool, PyBytes, PyCFunction, PyFloat, PyInt, PyModule};
    let py = value.py();
    let scalar = value.is_none() || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyInt>() || value.is_exact_instance_of::<PyBool>()
        || value.is_exact_instance_of::<PyFloat>() || value.is_exact_instance_of::<PyBytes>();
    if scalar && standard_receiver(method, "_SANDBOX_WRAP_STR_FORMAT", "_SANDBOX_WRAP_STR_FORMAT_CODE", "wrap_str_format")?.is_some() {
        let binding = crate::python_function::binding(py)?;
        let function = method.getattr(pyo3::intern!(py, "__func__"))?;
        let scope = FrameGlobals::of_function(&function)?;
        let instance = frame_global(&scope, pyo3::intern!(py, "isinstance"))?;
        if instance.is_exact_instance_of::<PyCFunction>()
            && instance.is(&binding.getattr(pyo3::intern!(py, "_BUILTIN_ISINSTANCE"))?)
            && instance.getattr(pyo3::intern!(py, "__name__"))?.eq("isinstance")?
            && instance.getattr(pyo3::intern!(py, "__module__"))?.eq("builtins")? {
            let types = frame_global(&scope, pyo3::intern!(py, "types"))?;
            if types.is_exact_instance_of::<PyModule>() {
                let attributes = types.cast::<PyModule>()?.dict();
                let method_type = attributes.get_item(pyo3::intern!(py, "MethodType"))?;
                let builtin_type = attributes.get_item(pyo3::intern!(py, "BuiltinMethodType"))?;
                if method_type.is_some_and(|kind| kind.is(&method.get_type()))
                    && builtin_type.is_some_and(|kind| kind.is(&py.get_type::<PyCFunction>())) {
                    return Ok(py.None().into_bound(py));
                }
            }
        }
    }
    method.call1((value,))
}

fn standard_receiver<'py>(
    method: &Bound<'py, PyAny>,
    function_key: &str,
    code_key: &str,
    name: &str,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = method.py();
    let binding = crate::python_function::binding(py)?;
    if method
        .get_type()
        .is(&binding.getattr(pyo3::intern!(py, "_METHOD_TYPE"))?)
        && crate::python_function::is_standard_function(
            &method.getattr(pyo3::intern!(py, "__func__"))?,
            &binding.getattr(function_key)?,
            &binding.getattr(code_key)?,
            binding
                .getattr(pyo3::intern!(py, "_SANDBOX_MODULE"))?
                .cast()?,
            name,
        )?
    {
        Ok(Some(method.getattr(pyo3::intern!(py, "__self__"))?))
    } else {
        Ok(None)
    }
}

fn unchanged_loop_class(class: &Bound<'_, PyAny>, baseline: &Bound<'_, PyTuple>, codes: &Bound<'_, PyTuple>) -> PyResult<bool> {
    let attributes = class.getattr("__dict__")?;
    if attributes.len()? != baseline.len() { return Ok(false); }
    for entry in baseline.iter() {
        let entry = entry.cast::<PyTuple>()?;
        let key = entry.get_item(0)?;
        let expected = entry.get_item(1)?;
        match attributes.get_item(&key) {
            Ok(value) if value.is(&expected) => {},
            Ok(_) => return Ok(false),
            Err(error) if error.is_instance_of::<pyo3::exceptions::PyKeyError>(class.py()) => return Ok(false),
            Err(error) => return Err(error),
        }
    }
    for entry in codes.iter() {
        let entry = entry.cast::<PyTuple>()?;
        if !entry.get_item(0)?.getattr("__code__")?.is(&entry.get_item(1)?) { return Ok(false); }
    }
    Ok(true)
}

fn guard_loop_classes(binding: &Bound<'_, PyAny>, asynchronous: bool) -> PyResult<bool> {
    let runtime = binding.getattr("_RUNTIME_MODULE")?;
    let loop_type = binding.getattr("_RUNTIME_LOOP_TYPE")?;
    if !runtime.getattr("LoopContext")?.is(&loop_type)
        || !unchanged_loop_class(&loop_type, &binding.getattr("_RUNTIME_LOOP_CLASS_ITEMS")?.cast_into::<PyTuple>()?,
            &binding.getattr("_RUNTIME_LOOP_CLASS_CODES")?.cast_into::<PyTuple>()?)? {
        return Ok(false);
    }
    if asynchronous {
        let async_type = binding.getattr("_RUNTIME_ASYNC_LOOP_TYPE")?;
        let bases = async_type.getattr("__bases__")?.cast_into::<PyTuple>()?;
        if !runtime.getattr("AsyncLoopContext")?.is(&async_type) || bases.len() != 1
            || !bases.get_item(0)?.is(&loop_type)
            || !unchanged_loop_class(&async_type, &binding.getattr("_RUNTIME_ASYNC_LOOP_CLASS_ITEMS")?.cast_into::<PyTuple>()?,
                &binding.getattr("_RUNTIME_ASYNC_LOOP_CLASS_CODES")?.cast_into::<PyTuple>()?)? {
            return Ok(false);
        }
    }
    Ok(true)
}

pub(crate) fn guard_internal_loop_class(binding: &Bound<'_, PyAny>, asynchronous: bool) -> PyResult<()> {
    if guard_loop_classes(binding, asynchronous)? { Ok(()) } else {
        Err(crate::python_bridge::unsupported(binding.py(),
            "modified loop class needs a runtime adapter"))
    }
}

fn guard_loop_method(binding: &Bound<'_, PyAny>, method_name: &str, asynchronous: bool,
    callable_policy: &Bound<'_, PyAny>) -> PyResult<bool> {
    if !guard_loop_classes(binding, asynchronous)? { return Ok(false); }
    let runtime = binding.getattr("_RUNTIME_MODULE")?;
    let loop_type = binding.getattr(if asynchronous { "_RUNTIME_ASYNC_LOOP_TYPE" } else { "_RUNTIME_LOOP_TYPE" })?;
    let mut standard = true;
    for (name, original, code) in [("changed", "_RUNTIME_LOOP_CHANGED", "_RUNTIME_LOOP_CHANGED_CODE"),
                                  ("cycle", "_RUNTIME_LOOP_CYCLE", "_RUNTIME_LOOP_CYCLE_CODE"),
                                  ("__call__", "_RUNTIME_LOOP_CALL", "_RUNTIME_LOOP_CALL_CODE")] {
        if method_name != "*" && method_name != name { continue; }
        let current = loop_type.getattr(name)?;
        let unchanged = crate::python_function::is_standard_function(&current, &binding.getattr(original)?,
            &binding.getattr(code)?, runtime.cast()?, name)?;
        standard &= unchanged;
        if unchanged {
            for flag in ["unsafe_callable", "alters_data"] {
                // A truth callback may replace the function's entire dict.
                let attributes = current.getattr("__dict__")?.cast_into::<PyDict>()?;
                let value = attributes.get_item(flag)?;
                drop(attributes);
                if let Some(value) = value {
                    // A flag whose truth test is user code is evaluated once,
                    // by the sandbox's own policy on the live LoopContext.
                    if !(value.is_exact_instance_of::<pyo3::types::PyBool>() || value.is_none()) {
                        return Ok(false);
                    }
                    // Standard bound methods forward these attributes to the
                    // function.
                    let truthy = value.is_truthy()?;
                    // The policy discards the first false result before the
                    // second global lookup; its finalizer can mutate globals.
                    drop(value);
                    if truthy { return Ok(false); }
                    if flag == "unsafe_callable" && !standard_callable_globals(binding, callable_policy)? {
                        return Ok(false);
                    }
                }
            }
            // Truth callbacks may replace a loop method or its code. Do not
            // dispatch the native implementation after such a mutation.
            if !guard_loop_classes(binding, asynchronous)? { return Ok(false); }
        }
    }
    Ok(standard)
}

fn standard_callable_globals(binding: &Bound<'_, PyAny>, method: &Bound<'_, PyAny>) -> PyResult<bool> {
    let function = method.getattr("__func__")?;
    let scope = FrameGlobals::of_function(&function)?;
    Ok(frame_global(&scope, pyo3::intern!(binding.py(), "getattr"))?.is(&binding.getattr("_BUILTIN_GETATTR")?))
}

fn standard_attribute_globals(binding: &Bound<'_, PyAny>, method: &Bound<'_, PyAny>) -> PyResult<bool> {
    let function = method.getattr("__func__")?;
    let scope = FrameGlobals::of_function(&function)?;
    let internal = frame_global(&scope, pyo3::intern!(binding.py(), "is_internal_attribute"))?;
    let sandbox = binding.getattr("_SANDBOX_MODULE")?;
    if !crate::python_function::is_standard_function(&internal, &binding.getattr("_SANDBOX_IS_INTERNAL_ATTRIBUTE")?,
        &binding.getattr("_SANDBOX_IS_INTERNAL_ATTRIBUTE_CODE")?, sandbox.cast()?, "is_internal_attribute")? {
        return Ok(false);
    }
    let scope = FrameGlobals::of_function(&internal)?;
    for (name, baseline) in [("isinstance", "_BUILTIN_ISINSTANCE"), ("hasattr", "_BUILTIN_HASATTR"),
                             ("type", "_BUILTIN_TYPE")] {
        if !frame_global(&scope, &PyString::new(binding.py(), name))?.is(&binding.getattr(baseline)?) { return Ok(false); }
    }
    let types = frame_global(&scope, pyo3::intern!(binding.py(), "types"))?;
    if !types.is(&binding.getattr("_SANDBOX_TYPES")?) { return Ok(false); }
    let names = binding.getattr("_SANDBOX_TYPE_NAMES")?.cast_into::<PyTuple>()?;
    let values = binding.getattr("_SANDBOX_TYPE_VALUES")?.cast_into::<PyTuple>()?;
    if names.len() != values.len() || values.iter().any(|value| value.is_none()) { return Ok(false); }
    for (name, baseline) in names.iter().zip(values.iter()) {
        if !types.getattr(name.cast::<PyString>()?)?.is(&baseline) { return Ok(false); }
    }
    Ok(true)
}

/// Recheck callable policy after evaluating call arguments, which can mutate it.
pub(crate) fn guard_internal_loop_call(call: &Bound<'_, PyAny>, method: &str, asynchronous: bool) -> PyResult<()> {
    if internal_loop_call_is_standard(call, method, asynchronous)? { Ok(()) }
    else { Err(crate::python_bridge::unsupported(call.py(),
        "custom sandbox policies for native loop objects need a runtime adapter")) }
}

/// Whether the sandbox call policy and the loop class are the originals, so
/// the native loop method may run without the live loop object.
pub(crate) fn internal_loop_call_is_standard(call: &Bound<'_, PyAny>, method: &str, asynchronous: bool) -> PyResult<bool> {
    let py = call.py();
    let binding = py.import("docxtpl_native.bridge")?;
    let Some(environment) = standard_receiver(call, "_SANDBOX_CALL", "_SANDBOX_CALL_CODE", "call")? else {
        return Ok(false);
    };
    let safe_callable = environment.getattr("is_safe_callable")?;
    Ok(standard_receiver(&safe_callable, "_SANDBOX_IS_SAFE_CALLABLE", "_SANDBOX_IS_SAFE_CALLABLE_CODE", "is_safe_callable")?.is_some()
        && standard_callable_globals(binding.as_any(), &safe_callable)?
        && guard_loop_method(binding.as_any(), method, asynchronous, &safe_callable)?)
}

/// Reject internal-loop lookups when user sandbox policies cannot yet receive
/// the native loop object. Never treat bypassing a policy as permission.
pub(crate) fn guard_internal_loop(
    lookup: &Bound<'_, PyAny>, attribute_first: bool, asynchronous: bool, post: bool,
) -> PyResult<()> {
    let py = lookup.py();
    let binding = py.import("docxtpl_native.bridge")?;
    let (normal, normal_code, name) = if attribute_first {
        ("_ENV_GETATTR", "_ENV_GETATTR_CODE", "getattr")
    } else { ("_ENV_GETITEM", "_ENV_GETITEM_CODE", "getitem") };
    if lookup.get_type().is(&binding.getattr("_METHOD_TYPE")?) {
        let function = lookup.getattr("__func__")?;
        if crate::python_function::is_standard_function(&function, &binding.getattr(normal)?,
            &binding.getattr(normal_code)?, binding.getattr("_ENVIRONMENT_MODULE")?.cast()?, name)? {
            if post { return Ok(()); }
            if guard_loop_classes(binding.as_any(), asynchronous)?
                && (!attribute_first || standard_callable_globals(binding.as_any(), lookup)?) {
                return Ok(());
            }
            return Err(crate::python_bridge::unsupported(py,
                "modified loop lookup or class needs a runtime adapter"));
        }
    }
    let (function, code, name) = if attribute_first {
        ("_SANDBOX_GETATTR", "_SANDBOX_GETATTR_CODE", "getattr")
    } else {
        ("_SANDBOX_GETITEM", "_SANDBOX_GETITEM_CODE", "getitem")
    };
    let Some(environment) = standard_receiver(lookup, function, code, name)? else {
        return Err(crate::python_bridge::unsupported(py,
            "custom lookups for native loop objects need a runtime adapter"));
    };
    let safe_attribute = environment.getattr("is_safe_attribute")?;
    let mut standard = standard_receiver(&safe_attribute, "_SANDBOX_IS_SAFE_ATTRIBUTE", "_SANDBOX_IS_SAFE_ATTRIBUTE_CODE", "is_safe_attribute")?.is_some();
    if standard && attribute_first && !post { standard = standard_callable_globals(binding.as_any(), lookup)?; }
    if standard { standard = standard_attribute_globals(binding.as_any(), &safe_attribute)?; }
    if standard { standard = guard_loop_classes(binding.as_any(), asynchronous)?; }
    if standard {
        let format = environment.getattr("wrap_str_format")?;
        standard = standard_receiver(&format, "_SANDBOX_WRAP_STR_FORMAT", "_SANDBOX_WRAP_STR_FORMAT_CODE", "wrap_str_format")?.is_some();
    }
    if standard { Ok(()) } else {
        Err(crate::python_bridge::unsupported(environment.py(),
            "custom sandbox policies for native loop objects need a runtime adapter"))
    }
}

#[pyfunction]
pub(crate) fn python_is_safe_callable<'py>(
    py: Python<'py>,
    environment: &Bound<'py, PyAny>,
    callback: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let method = environment.getattr(pyo3::intern!(py, "is_safe_callable"))?;
    if standard_receiver(
        &method,
        "_SANDBOX_IS_SAFE_CALLABLE",
        "_SANDBOX_IS_SAFE_CALLABLE_CODE",
        "is_safe_callable",
    )?
    .is_none()
    {
        return method.call1((callback,));
    }
    let scope = FrameGlobals::of_function(&method.getattr(pyo3::intern!(py, "__func__"))?)?;
    let first = frame_global(&scope, pyo3::intern!(py, "getattr"))?.call1((
        callback,
        pyo3::intern!(py, "unsafe_callable"),
        false,
    ))?;
    let selected = if first.is_truthy()? {
        first
    } else {
        // `or` releases a false first operand before evaluating the second;
        // its finalizer, like the descriptor or truth callback, may change
        // the getattr global.
        drop(first);
        frame_global(&scope, pyo3::intern!(py, "getattr"))?.call1((
            callback,
            pyo3::intern!(py, "alters_data"),
            false,
        ))?
    };
    // `not (first or second)` retains the first truthy operand, then tests
    // that object again for `not`. Stateful __bool__ must observe both calls.
    Ok((!selected.is_truthy()?)
        .into_pyobject(py)?
        .to_owned()
        .into_any())
}

fn internal_isinstance<'py>(scope: &FrameGlobals<'py>, object: &Bound<'py, PyAny>,
    name: &str) -> PyResult<bool> {
    let py = scope.py();
    let isinstance = frame_global(scope, pyo3::intern!(py, "isinstance"))?;
    let types = frame_global(scope, pyo3::intern!(py, "types"))?;
    let class = types.getattr(name)?;
    drop(types);
    isinstance.call1((object, class))?.is_truthy()
}

fn internal_contains<'py>(scope: &FrameGlobals<'py>, attribute: &Bound<'py, PyAny>,
    name: &str) -> PyResult<bool> {
    let container = frame_global(scope, &PyString::new(scope.py(), name))?;
    let result = unsafe { pyo3::ffi::PySequence_Contains(container.as_ptr(), attribute.as_ptr()) };
    if result < 0 { Err(PyErr::fetch(scope.py())) } else { Ok(result != 0) }
}

fn internal_attribute<'py>(function: &Bound<'py, PyAny>, object: &Bound<'py, PyAny>,
    attribute: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = function.py();
    let scope = FrameGlobals::of_function(function)?;
    let forbidden = if internal_isinstance(&scope, object, "FunctionType")? {
        internal_contains(&scope, attribute, "UNSAFE_FUNCTION_ATTRIBUTES")?
    } else if internal_isinstance(&scope, object, "MethodType")? {
        internal_contains(&scope, attribute, "UNSAFE_FUNCTION_ATTRIBUTES")?
            || internal_contains(&scope, attribute, "UNSAFE_METHOD_ATTRIBUTES")?
    } else if frame_global(&scope, pyo3::intern!(py, "isinstance"))?
        .call1((object, frame_global(&scope, pyo3::intern!(py, "type"))?))?.is_truthy()? {
        attribute.eq("mro")?
    } else {
        let isinstance = frame_global(&scope, pyo3::intern!(py, "isinstance"))?;
        let code = frame_global(&scope, pyo3::intern!(py, "types"))?.getattr("CodeType")?;
        let traceback = frame_global(&scope, pyo3::intern!(py, "types"))?.getattr("TracebackType")?;
        let frame = frame_global(&scope, pyo3::intern!(py, "types"))?.getattr("FrameType")?;
        let tuple = PyTuple::new(py, [code, traceback, frame])?;
        if isinstance.call1((object, tuple))?.is_truthy()? { true }
        else if internal_isinstance(&scope, object, "GeneratorType")? {
            internal_contains(&scope, attribute, "UNSAFE_GENERATOR_ATTRIBUTES")?
        } else {
            let has_coroutine = frame_global(&scope, pyo3::intern!(py, "hasattr"))?
                .call1((frame_global(&scope, pyo3::intern!(py, "types"))?, "CoroutineType"))?.is_truthy()?;
            if has_coroutine && internal_isinstance(&scope, object, "CoroutineType")? {
                internal_contains(&scope, attribute, "UNSAFE_COROUTINE_ATTRIBUTES")?
            } else {
                let has_async = frame_global(&scope, pyo3::intern!(py, "hasattr"))?
                    .call1((frame_global(&scope, pyo3::intern!(py, "types"))?, "AsyncGeneratorType"))?.is_truthy()?;
                if has_async && internal_isinstance(&scope, object, "AsyncGeneratorType")? {
                    internal_contains(&scope, attribute, "UNSAFE_ASYNC_GENERATOR_ATTRIBUTES")?
                } else { false }
            }
        }
    };
    if forbidden { return Ok(true.into_pyobject(py)?.to_owned().into_any()); }
    attribute.call_method1("startswith", ("__",))
}

pub(crate) fn safe_attribute<'py>(method: &Bound<'py, PyAny>, object: &Bound<'py, PyAny>,
    attribute: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = method.py();
    if standard_receiver(method, "_SANDBOX_IS_SAFE_ATTRIBUTE", "_SANDBOX_IS_SAFE_ATTRIBUTE_CODE", "is_safe_attribute")?.is_none() {
        return method.call1((object, attribute, value));
    }
    let function = method.getattr(pyo3::intern!(py, "__func__"))?;
    let scope = FrameGlobals::of_function(&function)?;
    let first = attribute.call_method1("startswith", ("_",))?;
    let selected = if first.is_truthy()? { first } else {
        drop(first);
        let inner = frame_global(&scope, pyo3::intern!(py, "is_internal_attribute"))?;
        let binding = crate::python_function::binding(py)?;
        let standard = crate::python_function::is_standard_function(&inner,
            &binding.getattr("_SANDBOX_IS_INTERNAL_ATTRIBUTE")?,
            &binding.getattr("_SANDBOX_IS_INTERNAL_ATTRIBUTE_CODE")?,
            binding.getattr("_SANDBOX_MODULE")?.cast()?, "is_internal_attribute")?;
        if standard { internal_attribute(&inner, object, attribute)? }
        else { inner.call1((object, attribute))? }
    };
    Ok((!selected.is_truthy()?).into_pyobject(py)?.to_owned().into_any())
}

#[pyfunction]
pub(crate) fn python_is_safe_attribute<'py>(py: Python<'py>, environment: &Bound<'py, PyAny>,
    object: &Bound<'py, PyAny>, attribute: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>)
    -> PyResult<Bound<'py, PyAny>> {
    safe_attribute(&environment.getattr(pyo3::intern!(py, "is_safe_attribute"))?, object, attribute, value)
}

#[pyfunction]
pub(crate) fn python_sandbox_call<'py>(
    py: Python<'py>,
    environment: &Bound<'py, PyAny>,
    context: &Bound<'py, PyAny>,
    callback: &Bound<'py, PyAny>,
    args: &Bound<'py, PyAny>,
    kwargs: &Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyAny>> {
    call_keywords(py, environment, context, callback, args, kwargs, None, None)
}

pub(crate) fn call_keywords<'py>(
    py: Python<'py>, environment: &Bound<'py, PyAny>, context: &Bound<'py, PyAny>,
    callback: &Bound<'py, PyAny>, args: &Bound<'py, PyAny>, kwargs: &Bound<'py, PyDict>,
    sources: Option<&Bound<'py, pyo3::types::PyList>>,
    prepared: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let method = match prepared { Some(value) => value.clone(), None => environment.getattr(pyo3::intern!(py, "call"))? };
    let merged = sources.map(|sources| crate::python_keywords::merge(&method, sources)).transpose()?;
    let kwargs = merged.as_ref().unwrap_or(kwargs);
    let args = crate::python_call::arguments(py, Vec::new(), args)?;
    let Some(receiver) = standard_receiver(&method, "_SANDBOX_CALL", "_SANDBOX_CALL_CODE", "call")?
    else {
        return method.call(
            crate::python_call::arguments(
                py,
                vec![context.clone(), callback.clone()],
                args.as_any(),
            )?,
            Some(kwargs),
        );
    };
    let kwargs = crate::python_call::bound_keywords(
        &method.getattr(pyo3::intern!(py, "__func__"))?, kwargs,
    )?;
    if !python_is_safe_callable(py, &receiver, callback)?.is_truthy()? {
        let scope = FrameGlobals::of_function(&method.getattr(pyo3::intern!(py, "__func__"))?)?;
        // Python resolves the exception constructor before evaluating !r.
        let constructor = frame_global(&scope, pyo3::intern!(py, "SecurityError"))?;
        let message = crate::python_operators::join_strings(
            py,
            vec![
                callback.repr()?.into_any(),
                PyString::new(py, " is not safely callable").into_any(),
            ],
            None,
        )?;
        return Err(PyErr::from_value(constructor.call1((message,))?));
    }
    crate::python_call::call_context_method(py, context, callback, args.as_any(), &kwargs)
}

pub(crate) fn call_operator<'py>(
    method: &Bound<'py, PyAny>,
    context: &Bound<'py, PyAny>,
    operator: &Bound<'py, PyString>,
    args: &Bound<'py, PyTuple>,
    unary: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let (function_key, code_key, name, table) = if unary {
        (
            "_SANDBOX_CALL_UNOP",
            "_SANDBOX_CALL_UNOP_CODE",
            "call_unop",
            "unop_table",
        )
    } else {
        (
            "_SANDBOX_CALL_BINOP",
            "_SANDBOX_CALL_BINOP_CODE",
            "call_binop",
            "binop_table",
        )
    };
    let Some(receiver) = standard_receiver(method, function_key, code_key, name)? else {
        let py = method.py();
        return method.call1(crate::python_call::arguments(
            py,
            vec![context.clone(), operator.clone().into_any()],
            args.as_any(),
        )?);
    };
    receiver.getattr(table)?.get_item(operator)?.call1(args)
}
