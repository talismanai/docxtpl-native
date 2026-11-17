//! `Context.blocks` as the registry the render uses.
//!
//! When the binding creates a template context, its `blocks` holds, for each
//! block of the template, a real generator function (`block_<name>`, an async
//! generator function in async renders) that renders that block natively, as
//! Jinja passes its compiled block functions. `extends` appends the parent's
//! functions. The VM's block dispatch runs these natively, calls anything a
//! constructor or callback put in their place, and raises `KeyError` when a
//! constructor or callback removed the block's entry.
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

use crate::python_bridge::Bridge;

/// The root template's blocks and, to run them outside the render (from a
/// context constructor or after the render), its environment and compilation.
pub(crate) struct RootBlocks {
    pub(crate) names: Vec<String>,
    pub(crate) template: Option<OwnedTemplate>,
}

type OwnedTemplate = Arc<(minijinja::Environment<'static>, minijinja::OwnedTemplate)>;

/// Renders one definition of a block: in the running render when there is
/// one, otherwise in a new state of the template with the given context.
#[pyclass(module = "docxtpl_native._native", frozen)]
pub(crate) struct NativeBlockRunner {
    name: String,
    depth: usize,
    bridge: Arc<Bridge>,
    template: Option<OwnedTemplate>,
}

#[pymethods]
impl NativeBlockRunner {
    #[pyo3(signature = (context, *_args))]
    fn __call__(&self, py: Python<'_>, context: &Bound<'_, PyAny>, _args: &Bound<'_, PyTuple>) -> PyResult<Py<PyAny>> {
        let value = if minijinja::has_host_state() {
            crate::python_async::without_reentrant_suspension(
                || minijinja::render_block_in_host_state(&self.name, self.depth))
        } else if let Some(template) = &self.template {
            let environment = context.getattr("environment")?;
            let root = self.bridge.root_value(py, context, &environment)?;
            let (env, compiled) = &**template;
            crate::python_async::without_reentrant_suspension(
                || env.template_from_owned(compiled).render_block_with_root(&self.name, self.depth, root))
        } else {
            return Err(crate::python_bridge::unsupported(py, "a block of this template can only run during its render"));
        }.map_err(|error| self.bridge.take_error().unwrap_or_else(|| crate::error(error)))?;
        self.bridge.python(py, &value)
    }
}

fn function<'py>(py: Python<'py>, bridge: &Arc<Bridge>, name: &str, depth: usize,
    template: Option<OwnedTemplate>) -> PyResult<Bound<'py, PyAny>> {
    let runner = Py::new(py, NativeBlockRunner { name: name.to_owned(), depth, bridge: bridge.clone(), template })?;
    crate::python_function::binding(py)?.getattr("_native_block_function")?
        .call1((name, runner, bridge.asynchronous))
}

/// The `blocks` mapping a new context receives: block name to its function.
pub(crate) fn initial_blocks<'py>(py: Python<'py>, bridge: &Arc<Bridge>, names: &[String]) -> PyResult<Bound<'py, PyDict>> {
    let blocks = PyDict::new(py);
    for name in names { blocks.set_item(name, function(py, bridge, name, 0, None)?)?; }
    Ok(blocks)
}

/// The root context's `blocks`, whose functions can also run outside the render.
pub(crate) fn root_blocks<'py>(py: Python<'py>, bridge: &Arc<Bridge>, blocks: &RootBlocks) -> PyResult<Bound<'py, PyDict>> {
    let mapping = PyDict::new(py);
    for name in &blocks.names { mapping.set_item(name, function(py, bridge, name, 0, blocks.template.clone())?)?; }
    Ok(mapping)
}

/// Record that `context`'s `blocks` is the authoritative registry.
pub(crate) fn mark_registry(py: Python<'_>, context: &Bound<'_, PyAny>) -> PyResult<()> {
    crate::python_function::binding(py)?.getattr("_NATIVE_BLOCK_REGISTRIES")?.call_method1("add", (context,))?;
    Ok(())
}

pub(crate) fn is_registry(py: Python<'_>, context: &Bound<'_, PyAny>) -> PyResult<bool> {
    crate::python_function::binding(py)?.getattr("_NATIVE_BLOCK_REGISTRIES")?.contains(context)
}

/// Whether `callable` is the native function for definition `depth` of `name`.
pub(crate) fn is_native(py: Python<'_>, callable: &Bound<'_, PyAny>, name: &str, depth: usize) -> PyResult<bool> {
    if !callable.is_exact_instance_of::<pyo3::types::PyFunction>() { return Ok(false); }
    let attributes = callable.getattr(pyo3::intern!(py, "__dict__"))?;
    let Ok(attributes) = attributes.cast_into::<PyDict>() else { return Ok(false); };
    let Some(runner) = attributes.get_item("_native_block")? else { return Ok(false); };
    let Ok(runner) = runner.cast::<NativeBlockRunner>() else { return Ok(false); };
    let runner = runner.get();
    Ok(runner.name == name && runner.depth == depth)
}

/// `extends` added the parent's definitions: append their functions.
pub(crate) fn loaded(py: Python<'_>, bridge: &Arc<Bridge>, context: &Bound<'_, PyAny>, appended: &[(&str, usize)]) -> PyResult<()> {
    if !is_registry(py, context)? { return Ok(()); }
    let blocks = context.getattr("blocks")?;
    for (name, depth) in appended {
        let stack = blocks.call_method1("setdefault", (*name, PyList::empty(py)))?;
        stack.call_method1("append", (function(py, bridge, name, *depth, None)?,))?;
    }
    Ok(())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeBlockRunner>()
}
