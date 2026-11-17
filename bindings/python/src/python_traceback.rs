//! Template frame in the traceback of a failing render, at the Python binding
//! boundary. Every bridge environment installs this observer: the exception
//! that leaves a render gets a `top-level template code` frame from
//! `<template>` whose locals are the template's lexical values. As in the
//! reference, that frame stays reachable from a garbage cycle of the helper's
//! own traceback, so the values outlive a cleared `__traceback__` until the
//! cycle collector runs.
use std::sync::Arc;
use minijinja::{Environment, State};
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::python_bridge::Bridge;

fn reconstruct(py: Python<'_>, bridge: &Bridge, state: &State, line: usize, original: &PyErr) -> PyResult<Option<PyErr>> {
    let _handled = crate::pyglobals::HandledException::new(py, original);
    let Some(locals) = bridge.traceback_locals(py, state)? else { return Ok(None); };
    if !locals.bind(py).is_exact_instance_of::<PyDict>() { return Ok(None); }
    let locals_dict = locals.bind(py).cast::<PyDict>()?;
    if locals_dict.contains("__jinja_exception__")? { locals_dict.del_item("__jinja_exception__")?; }
    let line = line.max(1);
    let source = format!("{}raise __jinja_exception__", "\n".repeat(line - 1));
    let globals = PyDict::new(py);
    globals.set_item("__name__", "<template>")?;
    globals.set_item("__file__", "<template>")?;
    globals.set_item("__jinja_exception__", original.value(py))?;
    let builtins = py.import("builtins")?;
    let code = builtins.getattr("compile")?.call1((source, "<template>", "exec"))?;
    let options = PyDict::new(py);
    options.set_item("co_name", "top-level template code")?;
    let code = code.call_method("replace", (), Some(&options))?;
    // The frames below the template frame: the callback's own traceback.
    // Python 3.11 can keep it in the error state rather than on the exception.
    let previous = original.traceback(py).map(|traceback| traceback.into_any());
    let helper = crate::python_function::binding(py)?.getattr("_traceback_probe_exec")?;
    let traceback = helper.call1((code, &globals, locals.bind(py)))?;
    // Python 3.12 chains the re-raised exception's traceback below the new
    // frame; 3.11 does not, so link it here.
    let unchained = traceback.getattr("tb_next")?.is_none();
    if let Some(previous) = previous.filter(|previous| unchained && !previous.is(&traceback)) {
        traceback.setattr("tb_next", previous)?;
    }
    if unsafe { pyo3::ffi::PyException_SetTraceback(original.value(py).as_ptr(), traceback.as_ptr()) } != 0 {
        return Err(PyErr::fetch(py));
    }
    Ok(Some(PyErr::from_value(original.value(py).clone().into_any())))
}

pub(crate) fn configure(environment: &mut Environment<'static>, bridge: &Arc<Bridge>) {
    let bridge = bridge.clone();
    environment.set_runtime_error_frame_observer(move |state, _, line| Python::attach(|py| {
        let Some(original) = bridge.peek_error(py) else { return; };
        if let Ok(Some(reconstructed)) = reconstruct(py, &bridge, state, line, &original) {
            bridge.replace_error(reconstructed);
        }
    }));
}
