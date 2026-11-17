//! Lookup decisions in Rust; CPython owns descriptors, mappings and exceptions.
use crate::pyglobals::{FrameGlobals, frame_global, handle_exception};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyModule};

fn undefined<'py>(
    environment: &Bound<'py, PyAny>,
    object: &Bound<'py, PyAny>,
    key: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = environment.py();
    let constructor = environment.getattr(pyo3::intern!(py, "undefined"))?;
    let kwargs = PyDict::new(py);
    kwargs.set_item(pyo3::intern!(py, "obj"), object)?;
    kwargs.set_item(pyo3::intern!(py, "name"), key)?;
    constructor.call((), Some(&kwargs))
}

fn attribute<'py>(
    scope: &FrameGlobals<'py>,
    object: &Bound<'py, PyAny>,
    key: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    frame_global(scope, pyo3::intern!(scope.py(), "getattr"))?.call1((object, key))
}

fn lookup<'py>(
    scope: &FrameGlobals<'py>,
    environment: &Bound<'py, PyAny>,
    object: &Bound<'py, PyAny>,
    key: &Bound<'py, PyAny>,
    attribute_first: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let py = scope.py();
    if attribute_first {
        match attribute(scope, object, key) {
            Ok(value) => return Ok(value),
            Err(error) => handle_exception(scope, error, &["AttributeError"], || Ok(()))?,
        }
        // The attribute handler has ended before item lookup starts.
        return match object.get_item(key) {
            Ok(value) => Ok(value),
            Err(error) => handle_exception(
                scope,
                error,
                &["TypeError", "LookupError", "AttributeError"],
                || undefined(environment, object, key),
            ),
        };
    }
    match object.get_item(key) {
        Ok(value) => Ok(value),
        Err(error) => handle_exception(
            scope,
            error,
            &["AttributeError", "TypeError", "LookupError"],
            || {
                let instance = frame_global(scope, pyo3::intern!(py, "isinstance"))?;
                let string = frame_global(scope, pyo3::intern!(py, "str"))?;
                if instance.call1((key, string))?.is_truthy()? {
                    // Resolve str again after isinstance: caller code may rebind it.
                    let converted =
                        (|| frame_global(scope, pyo3::intern!(py, "str"))?.call1((key,)))();
                    match converted {
                        Err(error) => handle_exception(scope, error, &["Exception"], || Ok(()))?,
                        Ok(key) => match attribute(scope, object, &key) {
                            Ok(value) => return Ok(value),
                            Err(error) => handle_exception(scope, error, &["AttributeError"], || Ok(()))?,
                        },
                    }
                }
                undefined(environment, object, key)
            },
        ),
    }
}

/// Jinja's sandbox prefers item lookup, then checks any attribute fallback.
/// Policy methods remain live callbacks on the actual environment receiver.
fn sandbox_item_lookup<'py>(scope: &FrameGlobals<'py>, environment: &Bound<'py, PyAny>,
    object: &Bound<'py, PyAny>, key: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = scope.py();
    let recovered = match object.get_item(key) {
        Ok(value) => return Ok(value),
        Err(error) => handle_exception(scope, error, &["TypeError", "LookupError"], || {
            let instance = frame_global(scope, pyo3::intern!(py, "isinstance"))?;
            let string = frame_global(scope, pyo3::intern!(py, "str"))?;
            if !instance.call1((key, string))?.is_truthy()? { return Ok(None); }
            let converted = match (|| frame_global(scope, pyo3::intern!(py, "str"))?.call1((key,)))() {
                Ok(value) => Some(value),
                Err(error) => handle_exception(scope, error, &["Exception"], || Ok(None))?,
            };
            let Some(converted) = converted else { return Ok(None); };
            let value = match attribute(scope, object, &converted) {
                Ok(value) => Some(value),
                Err(error) => handle_exception(scope, error, &["AttributeError"], || Ok(None))?,
            };
            let Some(value) = value else { return Ok(None); };
            Ok(Some(sandbox_attribute_policy(environment, object, key, &value)?))
        })?,
    };
    match recovered { Some(value) => Ok(value), None => undefined(environment, object, key) }
}

fn sandbox_attribute_policy<'py>(environment: &Bound<'py, PyAny>, object: &Bound<'py, PyAny>,
    key: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = environment.py();
    let formatter = environment.getattr(pyo3::intern!(py, "wrap_str_format"))?;
    let format = crate::python_sandbox::wrap_str_format(&formatter, value)?;
    drop(formatter);
    if !format.is_none() { return Ok(format); }
    let policy = environment.getattr(pyo3::intern!(py, "is_safe_attribute"))?;
    if crate::python_sandbox::safe_attribute(&policy, object, key, value)?.is_truthy()? {
        return Ok(value.clone());
    }
    environment.getattr(pyo3::intern!(py, "unsafe_undefined"))?.call1((object, key))
}

/// Sandbox attribute lookup falls back to item access only after the caught
/// AttributeError, and checks policies only when attribute lookup succeeded.
fn sandbox_attribute_lookup<'py>(scope: &FrameGlobals<'py>, environment: &Bound<'py, PyAny>,
    object: &Bound<'py, PyAny>, key: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    match attribute(scope, object, key) {
        Ok(value) => sandbox_attribute_policy(environment, object, key, &value),
        Err(error) => {
            let recovered = handle_exception(scope, error, &["AttributeError"], || {
                match object.get_item(key) {
                    Ok(value) => Ok(Some(value)),
                    Err(error) => handle_exception(scope, error, &["TypeError", "LookupError"], || Ok(None)),
                }
            })?;
            match recovered { Some(value) => Ok(value), None => undefined(environment, object, key) }
        }
    }
}

#[pyfunction]
pub(crate) fn python_lookup<'py>(
    py: Python<'py>,
    environment: &Bound<'py, PyAny>,
    object: &Bound<'py, PyAny>,
    key: &Bound<'py, PyAny>,
    attribute_first: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let method = lookup_method(environment, attribute_first)?;
    let binding = py.import("docxtpl_native.bridge")?;
    dispatch_lookup(&method, object, key, attribute_first, &binding)
}

pub(crate) fn lookup_with_binding<'py>(
    environment: &Bound<'py, PyAny>,
    object: &Bound<'py, PyAny>,
    key: &Bound<'py, PyAny>,
    attribute_first: bool,
    binding: &Bound<'py, PyModule>,
) -> PyResult<Bound<'py, PyAny>> {
    let method = lookup_method(environment, attribute_first)?;
    dispatch_lookup(&method, object, key, attribute_first, binding)
}

fn lookup_method<'py>(
    environment: &Bound<'py, PyAny>,
    attribute_first: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let py = environment.py();
    environment.getattr(if attribute_first {
        pyo3::intern!(py, "getattr")
    } else {
        pyo3::intern!(py, "getitem")
    })
}

pub(crate) fn dispatch_lookup<'py>(
    method: &Bound<'py, PyAny>,
    object: &Bound<'py, PyAny>,
    key: &Bound<'py, PyAny>,
    attribute_first: bool,
    binding: &Bound<'py, PyModule>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = binding.py();
    let (original_key, code_key) = if attribute_first {
        (
            pyo3::intern!(py, "_ENV_GETATTR"),
            pyo3::intern!(py, "_ENV_GETATTR_CODE"),
        )
    } else {
        (
            pyo3::intern!(py, "_ENV_GETITEM"),
            pyo3::intern!(py, "_ENV_GETITEM_CODE"),
        )
    };
    if method
        .get_type()
        .is(&binding.getattr(pyo3::intern!(py, "_METHOD_TYPE"))?)
    {
        let function = method.getattr(pyo3::intern!(py, "__func__"))?;
        if crate::python_function::is_standard_function(
            &function,
            &binding.getattr(original_key)?,
            &binding.getattr(code_key)?,
            binding
                .getattr(pyo3::intern!(py, "_ENVIRONMENT_MODULE"))?
                .cast()?,
            if attribute_first {
                "getattr"
            } else {
                "getitem"
            },
        )? {
            let scope = FrameGlobals::of_function(&function)?;
            // A bound standard method may have been installed from another
            // Environment. Its receiver still owns undefined and other policy.
            return lookup(
                &scope,
                &method.getattr(pyo3::intern!(py, "__self__"))?,
                object,
                key,
                attribute_first,
            );
        }
        let (sandbox_function, sandbox_code, sandbox_name) = if attribute_first {
            ("_SANDBOX_GETATTR", "_SANDBOX_GETATTR_CODE", "getattr")
        } else { ("_SANDBOX_GETITEM", "_SANDBOX_GETITEM_CODE", "getitem") };
        if crate::python_function::is_standard_function(
            &function,
            &binding.getattr(sandbox_function)?,
            &binding.getattr(sandbox_code)?,
            binding.getattr(pyo3::intern!(py, "_SANDBOX_MODULE"))?.cast()?,
            sandbox_name,
        )? {
            let scope = FrameGlobals::of_function(&function)?;
            let receiver = method.getattr(pyo3::intern!(py, "__self__"))?;
            return if attribute_first { sandbox_attribute_lookup(&scope, &receiver, object, key) }
                else { sandbox_item_lookup(&scope, &receiver, object, key) };
        }
    }
    // Custom methods retain their own policies and callbacks.
    method.call1((object, key))
}
