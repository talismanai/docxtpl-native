//! Small public document APIs with caller-observable object protocols.
use pyo3::prelude::*;
use pyo3::types::PyDict;

use super::crc_binding::with_context_manager;
use super::pyglobals::{frame_global, FrameGlobals};

/// Save the exposed graph once, preserving hooks and the original destination.
/// The model's serializer remains an explicit Python migration boundary.
#[pyfunction]
pub(crate) fn save_exposed_model<'py>(tpl: &Bound<'py, PyAny>, filename: &Bound<'py, PyAny>)
    -> PyResult<Option<Bound<'py, pyo3::types::PyBytes>>> {
    let py = tpl.py();
    tpl.call_method0(pyo3::intern!(py, "pre_processing"))?;
    let document = tpl.getattr(pyo3::intern!(py, "docx"))?;
    // With python-docx's standard save chain the archive is built natively;
    // the caller writes it as the unexposed path does, then post-processes.
    if let Some(archive) = crate::exposed_save::archive(&document, filename)? { return Ok(Some(archive)); }
    document.call_method1(pyo3::intern!(py, "save"), (filename,))?;
    tpl.call_method1(pyo3::intern!(py, "post_processing"), (filename,))?;
    tpl.setattr(pyo3::intern!(py, "is_saved"), true)?;
    Ok(None)
}

#[pyfunction]
pub(crate) fn render_init(tpl: &Bound<'_, PyAny>, index: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = tpl.py();
    tpl.call_method0(pyo3::intern!(py, "init_docx"))?;
    tpl.setattr(pyo3::intern!(py, "pic_map"), PyDict::new(py))?;
    tpl.setattr(pyo3::intern!(py, "current_rendering_part"), py.None())?;
    tpl.setattr(pyo3::intern!(py, "docx_ids_index"), index)?;
    tpl.setattr(pyo3::intern!(py, "is_saved"), false)?;
    Ok(())
}

#[pyfunction]
pub(crate) fn get_xml<'py>(tpl: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = tpl.py();
    let serializer = tpl.getattr(pyo3::intern!(py, "xml_to_string"))?;
    let body = tpl.getattr(pyo3::intern!(py, "docx"))?.getattr(pyo3::intern!(py, "_element"))?.getattr(pyo3::intern!(py, "body"))?;
    serializer.call1((body,))
}

#[pyfunction]
pub(crate) fn write_xml(tpl: &Bound<'_, PyAny>, filename: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = tpl.py();
    let scope = FrameGlobals::current(py)?;
    let manager = frame_global(&scope, pyo3::intern!(py, "open"))?
        .call1((filename, pyo3::intern!(py, "w")))?;
    with_context_manager(&manager, |file| {
        let write = file.getattr(pyo3::intern!(py, "write"))?;
        let xml = tpl.call_method0(pyo3::intern!(py, "get_xml"))?;
        write.call1((xml,))?;
        Ok(())
    })?;
    Ok(())
}
