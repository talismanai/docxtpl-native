//! Finalization and output conversion for Python-owned values in the Rust VM.
//!
//! The VM owns formatting decisions; foreign object types, user callbacks and
//! their exceptions remain CPython protocols. Escaping shares the concat path.
use pyo3::exceptions::PyAttributeError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyFloat, PyInt, PyString};

pub(crate) fn optional_attribute<'py>(value: &Bound<'py, PyAny>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    match value.getattr(name) {
        Ok(value) => Ok(value),
        Err(error) if error.is_instance_of::<PyAttributeError>(value.py()) => {
            Ok(value.py().None().into_bound(value.py()))
        }
        Err(error) => Err(error),
    }
}

#[pyfunction]
#[pyo3(signature = (config, value, visible, autoescape, name=None, prepared=None, has_prepared=false))]
pub(crate) fn python_format_value<'py>(
    py: Python<'py>,
    config: &Bound<'py, PyAny>,
    value: &Bound<'py, PyAny>,
    visible: &Bound<'py, PyAny>,
    autoescape: bool,
    name: Option<&str>,
    prepared: Option<&Bound<'py, PyAny>>,
    has_prepared: bool,
) -> PyResult<Bound<'py, PyAny>> {
    format_value(py, config, value, visible, autoescape, name, prepared, has_prepared, None, None)
}

pub(crate) fn format_value<'py>(
    py: Python<'py>, config: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>,
    visible: &Bound<'py, PyAny>, autoescape: bool, name: Option<&str>,
    prepared: Option<&Bound<'py, PyAny>>, has_prepared: bool,
    runtime_context: Option<&Bound<'py, PyAny>>,
    compiled_plan: Option<(bool, &Bound<'py, PyAny>)>,
) -> PyResult<Bound<'py, PyAny>> {
    let value = finalize_value(py, config, value, visible, autoescape, name, prepared,
        has_prepared, runtime_context, compiled_plan)?;
    stringify_value(&value, autoescape)
}

pub(crate) fn finalize_value<'py>(
    py: Python<'py>, config: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>,
    visible: &Bound<'py, PyAny>, autoescape: bool, name: Option<&str>,
    prepared: Option<&Bound<'py, PyAny>>, has_prepared: bool,
    runtime_context: Option<&Bound<'py, PyAny>>,
    compiled_plan: Option<(bool, &Bound<'py, PyAny>)>,
) -> PyResult<Bound<'py, PyAny>> {
    if !(value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyFloat>()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_exact_instance_of::<PyBytes>()
        || value.is_none()
        || crate::live_routing::plain_package_value(value)?)
    {
        config.setattr("_simple_names", py.None())?;
    }
    let environment = config.getattr("environment")?;
    let (present, kind) = if let Some((present, kind)) = compiled_plan {
        (present, kind.clone())
    } else if let Some(name) = name {
        let plan = config.getattr("_compile_finalizers")?.get_item(name)?;
        (plan.get_item(0)?.is_truthy()?, plan.get_item(1)?)
    } else {
        let callback = environment.getattr("finalize")?;
        let present = callback.is_truthy()?;
        let kind = if present {
            optional_attribute(&optional_attribute(&callback, "jinja_pass_arg")?, "name")?
        } else {
            py.None().into_bound(py)
        };
        (present, kind)
    };
    let mut value = value.clone();
    if present {
        let callback = if has_prepared {
            prepared
                .cloned()
                .unwrap_or_else(|| py.None().into_bound(py))
        } else {
            environment.getattr("finalize")?
        };
        value = if kind.eq("context")? {
            callback.call1((
                match runtime_context { Some(context) => context.clone(),
                    None => crate::python_context::python_context(py, config, visible, autoescape)? },
                &value,
            ))?
        } else if kind.eq("environment")? {
            callback.call1((environment, &value))?
        } else if kind.eq("eval_context")? {
            let context = match runtime_context { Some(context) => context.getattr("eval_ctx")?, None => {
                let context = crate::python_context::python_eval_context(py, &environment)?;
                context.setattr("autoescape", autoescape)?;
                context
            }};
            callback.call1((context, &value))?
        } else {
            callback.call1((&value,))?
        };
    }
    Ok(value)
}

pub(crate) fn stringify_value<'py>(value: &Bound<'py, PyAny>, autoescape: bool) -> PyResult<Bound<'py, PyAny>> {
    if let Some(text) = loop_text(value)? {
        let text = text.into_any();
        return if autoescape { crate::python_operators::escape_value(&text) } else { Ok(text) };
    }
    let value = if autoescape { crate::python_operators::escape_value(value)? } else { value.clone() };
    Ok(value.str()?.into_any())
}

/// `LoopContext.__repr__` computed here when a loop object keeps the original
/// `__repr__` (captured when the binding was imported), `__str__` and no `__html__`: `<Type index/length>`, with each
/// part formatted as the f-string in the reference formats it.
fn loop_text<'py>(value: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, pyo3::types::PyString>>> {
    let py = value.py();
    let binding = crate::python_function::binding(py)?;
    let loop_type = binding.getattr("_RUNTIME_LOOP_TYPE")?;
    let kind = value.get_type();
    // type(value), not isinstance: that would read a user-defined __class__.
    if unsafe { pyo3::ffi::PyType_IsSubtype(kind.as_type_ptr(), loop_type.as_ptr().cast()) } == 0 {
        return Ok(None);
    }
    let object = py.import("builtins")?.getattr("object")?;
    let original = binding.getattr("_RUNTIME_LOOP_REPR")?;
    if !kind.getattr("__repr__")?.is(&original)
        || !kind.getattr("__str__")?.is(&object.getattr("__str__")?)
        || value.hasattr("__html__")? {
        return Ok(None);
    }
    let format = py.import("builtins")?.getattr("format")?;
    // The reference resolves `type` in its module's globals at call time.
    let scope = crate::pyglobals::FrameGlobals::of_function(&original)?;
    let named = crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "type"))?.call1((value,))?;
    let name = format.call1((named.getattr("__name__")?, ""))?;
    let index = format.call1((value.getattr("index")?, ""))?;
    let length = format.call1((value.getattr("length")?, ""))?;
    let text = format!("<{} {}/{}>", name.str()?, index.str()?, length.str()?);
    Ok(Some(pyo3::types::PyString::new(py, &text)))
}
