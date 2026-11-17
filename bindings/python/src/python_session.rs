//! Native execution for a caller-owned Python document graph. No document core
//! or libxml node pointers cross into callbacks; the owner commits each phase.
use crate::{error, python_bridge};
use minijinja::{AutoEscape, Environment, value::Value};
use pyo3::prelude::*;
use pyo3::IntoPyObjectExt;
use pyo3::types::PyDict;
use std::sync::{Arc, atomic::Ordering};

pub(crate) fn join_fragments(
    bridge: &Arc<python_bridge::Bridge>, parts: Vec<Value>, autoescape: AutoEscape,
) -> Result<Value, minijinja::Error> {
    Python::attach(|py| -> PyResult<Value> {
            let values = parts.iter().map(|part| bridge.python(py, part).map(|value| value.into_bound(py)))
                .collect::<PyResult<Vec<_>>>()?;
            let mut result = crate::python_operators::join_strings(py, values, None)?;
            if !matches!(autoescape, AutoEscape::None) {
                result = py.import("jinja2.runtime")?.getattr("Markup")?.call1((result,))?;
            }
            Ok(bridge.opaque_value(&result))
        }).map_err(|error| bridge.remember(error))
}

fn render_fragments(
    environment: &Environment<'_>, template: &minijinja::OwnedTemplate, context: Value, bridge: &Arc<python_bridge::Bridge>,
) -> Result<Py<PyAny>, minijinja::Error> {
    let joiner = environment.fragment_joiner().expect("Python fragment joiner configured");
    let result = environment.template_from_owned(template).render_fragments(context, joiner)?;
    // After the render, possibly on another thread (see fiber_local).
    crate::fiber_local::attach(|py| bridge.python(py, &result)).map_err(|error| bridge.remember(error))
}

#[pyclass]
pub struct PythonRenderSession {
    bridge: Arc<python_bridge::Bridge>,
    environment: Environment<'static>,
}

#[pymethods]
impl PythonRenderSession {
    #[new]
    pub(crate) fn new(configuration: Py<PyAny>, context: Py<PyAny>) -> PyResult<Self> {
        let core = docxtplrs::template::TplCore::default();
        let bridge = python_bridge::Bridge::new(configuration, context, false, false)?;
        let mut environment = docxtplrs::template::make_env(false, &core);
        jinja::add_jinja2_compat(&mut environment);
        python_bridge::configure(&mut environment, &bridge)?;
        environment.enable_shared_loaded_cache();
        Ok(Self { bridge, environment })
    }

    pub(crate) fn render(&self, py: Python<'_>, name: String, source: String) -> PyResult<Py<PyAny>> {
        self.bridge.with_error_scope(|| {
            let environment = self.environment.clone();
            if self.bridge.asynchronous {
                // Compilation occurs before asyncio.run in Template.render too.
                let compiled = py.detach(|| environment.compile_owned_template(name, source).map_err(error));
                if let Some(original) = self.bridge.take_error() { return Err(original); }
                let compiled = compiled?;
                let bridge = self.bridge.clone();
                let blocks = crate::python_blocks::RootBlocks {
                    names: environment.template_from_owned(&compiled).block_names().map(str::to_owned).collect(),
                    template: Some(Arc::new((environment.clone(), compiled.clone()))),
                };
                return crate::python_async::run(py, bridge.clone(), move || {
                    let context = bridge.snapshot_with_blocks(Some(blocks))?;
                    let rendered = render_fragments(&environment, &compiled, context, &bridge).map_err(error);
                    if let Some(original) = bridge.take_error() {
                        // After the render, possibly on another thread (see fiber_local).
                        crate::python_async::release(rendered);
                        return Err(crate::fiber_local::attach(move |py| crate::python_errors::escaped_iteration(
                            py, original, crate::python_errors::Boundary::AsyncGenerator)));
                    }
                    rendered
                });
            }
            py.detach(|| {
                let mut runtime_started = false;
                let rendered = (|| {
                    if let Some(text) = self.bridge.preprocess_plain_extension(&source)? {
                        self.bridge.snapshot()?;
                        return Python::attach(|py| text.into_py_any(py));
                    }
                    // from_string sources are compiled afresh in the reference.
                    // Keep the loader environment alive across part boundaries.
                    let compiled = environment.compile_owned_template(name, source).map_err(error)?;
                    let blocks = crate::python_blocks::RootBlocks {
                        names: environment.template_from_owned(&compiled).block_names().map(str::to_owned).collect(),
                        template: Some(Arc::new((environment.clone(), compiled.clone()))),
                    };
                    let context = self.bridge.snapshot_with_blocks(Some(blocks))?;
                    runtime_started = true;
                    render_fragments(&environment, &compiled, context, &self.bridge).map_err(error)
                })();
                if let Some(original) = self.bridge.take_error() {
                    return Err(if runtime_started {
                        Python::attach(|py| crate::python_errors::escaped_iteration(
                            py, original, crate::python_errors::Boundary::Generator))
                    } else { original });
                }
                rendered
            })
        })
    }

    // Kept for the probe tests: configure() already installs the observer.
    fn _enable_traceback_probe(&mut self) {
        crate::python_traceback::configure(&mut self.environment, &self.bridge);
    }

    fn _native_block_names(&self, py: Python<'_>, source: String) -> PyResult<Vec<String>> {
        self.bridge.with_error_scope(|| {
            let compiled = py.detach(|| self.environment.compile_owned_template("<string>".into(), source).map_err(error));
            if let Some(original) = self.bridge.take_error() { return Err(original); }
            let compiled = compiled?;
            Ok(self.environment.template_from_owned(&compiled).block_names().map(str::to_owned).collect())
        })
    }

    fn _enable_await_adapter_probe(&self) {
        self.bridge.await_adapter_probe.store(true, std::sync::atomic::Ordering::Relaxed);
    }

    fn statistics(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let result = PyDict::new(py);
        result.set_item("renderer", "native")?;
        result.set_item("template_runtime", "MiniJinja+rsyi/jinja")?;
        result.set_item("context_bridge", "CPython references")?;
        result.set_item("document_model", "python-docx/lxml")?;
        result.set_item("python_callback_calls", self.bridge.callbacks.load(Ordering::Relaxed))?;
        result.set_item("python_formatter_calls", 0)?;
        result.set_item("native_value_formatter_calls", self.bridge.formats.load(Ordering::Relaxed))?;
        result.set_item("native_string_formatter_calls", self.bridge.fast_formats.load(Ordering::Relaxed))?;
        Ok(result.unbind())
    }
}

#[pyfunction]
pub fn finish_python_body(source: &Bound<'_, pyo3::types::PyString>, mut next_id: u32) -> PyResult<(String, u32)> {
    let recovered;
    let text = match source.to_str() {
        Ok(text) => text,
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyUnicodeEncodeError>(source.py()) => {
            recovered = crate::recover_lxml_unicode(source.py(), source)?;
            &recovered
        }
        Err(error) => return Err(error),
    };
    let fixed = docxtplrs::template::fix_tables_and_docpr(text, &mut next_id).map_err(error)?;
    Ok((fixed, next_id))
}

#[pyfunction]
pub fn resolve_listing_xml(source: &str) -> String {
    docxtplrs::patch::resolve_listing(source).into_owned()
}
