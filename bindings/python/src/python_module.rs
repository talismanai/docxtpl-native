//! The module an `import` creates, as `jinja2.environment.TemplateModule`.
//!
//! Its text is the module's rendered body, written natively while the class,
//! its `__str__`/`__html__` and `concat` are the originals. When the module
//! crosses into Python (a callback, a filter, a context mapping), or at the
//! import when `TemplateModule` was replaced, it becomes a real
//! `TemplateModule` built by the current class with the exports and the body
//! fragments as `_body_stream`; the same object is used afterwards.
use std::sync::Arc;

use minijinja::value::Value;
use minijinja::RuntimeModule;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::python_bridge::Bridge;

/// The `TemplateModule` for `module`, created on first use.
pub(crate) fn foreign_module(py: Python<'_>, bridge: &Arc<Bridge>, module: &Arc<RuntimeModule>) -> PyResult<Py<PyAny>> {
    if let Some(value) = module.foreign_value() { return bridge.python(py, value); }
    let exported = PyDict::new(py);
    for (key, value) in module.exported_items() {
        exported.set_item(bridge.python(py, &key)?, bridge.python(py, &value)?)?;
    }
    let body = PyList::empty(py);
    for fragment in module.fragments() { body.append(bridge.python(py, fragment)?)?; }
    let binding = crate::python_function::binding(py)?;
    let object = binding.getattr("_native_template_module")?.call1((module.template_name(), exported, body))?;
    let value = module.set_foreign_value(bridge.opaque_value(&object));
    bridge.python(py, value)
}

/// The module's text, when writing it natively is equivalent to the reference.
pub(crate) fn native_text(py: Python<'_>, module: &Arc<RuntimeModule>, bridge: &Arc<Bridge>) -> PyResult<Option<Value>> {
    let binding = crate::python_function::binding(py)?;
    let (class, instance) = match module.foreign_value() {
        Some(value) => {
            let object = bridge.python(py, value)?;
            (object.bind(py).get_type().into_any(), object.into_bound(py))
        }
        None => (binding.getattr("_ENVIRONMENT_MODULE")?.getattr("TemplateModule")?, py.None().into_bound(py)),
    };
    let standard = binding.getattr("_native_module_text_is_standard")?.call1((class, instance))?.is_truthy()?;
    Ok(standard.then(|| module.captured().clone()))
}

/// At the import: a replaced `TemplateModule` is constructed now, as Jinja does.
pub(crate) fn created(py: Python<'_>, bridge: &Arc<Bridge>, module: &Value) -> PyResult<()> {
    let Some(module) = module.downcast_object::<RuntimeModule>() else { return Ok(()); };
    let binding = crate::python_function::binding(py)?;
    let current = binding.getattr("_ENVIRONMENT_MODULE")?.getattr("TemplateModule")?;
    if current.is(&binding.getattr("_ORIGINAL_TEMPLATE_MODULE")?) { return Ok(()); }
    foreign_module(py, bridge, &module).map(|_| ())
}
