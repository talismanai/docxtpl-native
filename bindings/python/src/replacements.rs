//! Caller-visible replacement APIs and their Python file protocols.
use pyo3::prelude::*;
use pyo3::types::PyString;

use super::crc_binding::with_context_manager;
use super::pyglobals::{frame_global, FrameGlobals};

fn assign<'py>(tpl: &Bound<'py, PyAny>, mapping: &Bound<'py, PyString>, key: &Bound<'py, PyAny>, data: &Bound<'py, PyAny>) -> PyResult<()> {
    tpl.getattr(mapping)?.set_item(key, data)
}

fn open_destination<'py>(scope: &FrameGlobals<'py>, destination: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = scope.py();
    frame_global(scope, pyo3::intern!(py, "open"))?.call1((destination, pyo3::intern!(py, "rb")))
}

fn replace_stream_or_path<'py>(tpl: &Bound<'py, PyAny>, key: &Bound<'py, PyAny>, destination: &Bound<'py, PyAny>, mapping: &Bound<'py, PyString>, scope: &FrameGlobals<'py>) -> PyResult<()> {
    let py = tpl.py();
    let hasattr = frame_global(scope, pyo3::intern!(py, "hasattr"))?;
    if hasattr.call1((destination, pyo3::intern!(py, "read")))?.is_truthy()? {
        let data = destination.call_method0(pyo3::intern!(py, "read"))?;
        assign(tpl, mapping, key, &data)
    } else {
        let manager = open_destination(scope, destination)?;
        with_context_manager(&manager, |file| {
            let data = file.call_method0(pyo3::intern!(py, "read"))?;
            assign(tpl, mapping, key, &data)
        })?;
        Ok(())
    }
}

#[pyfunction]
pub(crate) fn replace_media(tpl: &Bound<'_, PyAny>, source: &Bound<'_, PyAny>, destination: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = tpl.py();
    let scope = FrameGlobals::current(py)?;
    let crc = tpl.call_method1(pyo3::intern!(py, "get_file_crc"), (source,))?;
    replace_stream_or_path(tpl, &crc, destination, pyo3::intern!(py, "crc_to_new_media"), &scope)
}

#[pyfunction]
pub(crate) fn replace_pic(tpl: &Bound<'_, PyAny>, name: &Bound<'_, PyAny>, destination: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = tpl.py();
    let scope = FrameGlobals::current(py)?;
    replace_stream_or_path(tpl, name, destination, pyo3::intern!(py, "pics_to_replace"), &scope)
}

#[pyfunction]
pub(crate) fn replace_embedded(tpl: &Bound<'_, PyAny>, source: &Bound<'_, PyAny>, destination: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = tpl.py();
    let scope = FrameGlobals::current(py)?;
    let manager = open_destination(&scope, destination)?;
    with_context_manager(&manager, |file| {
        let crc = tpl.call_method1(pyo3::intern!(py, "get_file_crc"), (source,))?;
        let data = file.call_method0(pyo3::intern!(py, "read"))?;
        assign(tpl, pyo3::intern!(py, "crc_to_new_embedded"), &crc, &data)
    })?;
    Ok(())
}

#[pyfunction]
pub(crate) fn replace_zipname(tpl: &Bound<'_, PyAny>, name: &Bound<'_, PyAny>, destination: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = tpl.py();
    let scope = FrameGlobals::current(py)?;
    let manager = open_destination(&scope, destination)?;
    with_context_manager(&manager, |file| {
        let data = file.call_method0(pyo3::intern!(py, "read"))?;
        assign(tpl, pyo3::intern!(py, "zipname_to_replace"), name, &data)
    })?;
    Ok(())
}
