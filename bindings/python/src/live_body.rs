//! Interact with the exposed body using the original Python object protocols.
use crate::pyglobals::{FrameGlobals, frame_global};
use pyo3::prelude::*;
use pyo3::types::PyDict;

#[pyfunction]
pub(crate) fn render_live_body(document: &Bound<'_, PyAny>, render_xml: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let py = document.py();
    let serialize = document.getattr(pyo3::intern!(py, "xml_to_string"))?;
    let body = document.getattr(pyo3::intern!(py, "docx"))?
        .getattr(pyo3::intern!(py, "_element"))?
        .getattr(pyo3::intern!(py, "body"))?;
    let source = serialize.call1((body,))?;
    let part = document.getattr(pyo3::intern!(py, "docx"))?
        .getattr(pyo3::intern!(py, "_part"))?;
    Ok(render_xml.call1((source, part))?.unbind())
}

#[pyfunction]
/// The committed body element is returned: the caller's frame holds it as
/// docxtpl's `render` holds `tree`, so after a failure in a later phase the
/// body stays the parsed (plain lxml) element while the traceback lives.
pub(crate) fn commit_live_body<'py>(document: &Bound<'py, PyAny>, body: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = document.py();
    let scope = FrameGlobals::current(py)?;
    let etree = frame_global(&scope, pyo3::intern!(py, "etree"))?;
    let fromstring = etree.getattr(pyo3::intern!(py, "fromstring"))?;
    let etree = frame_global(&scope, pyo3::intern!(py, "etree"))?;
    let parser_factory = etree.getattr(pyo3::intern!(py, "XMLParser"))?;
    let options = PyDict::new(py);
    options.set_item(pyo3::intern!(py, "recover"), true)?;
    let parser = parser_factory.call((), Some(&options))?;
    let options = PyDict::new(py);
    options.set_item(pyo3::intern!(py, "parser"), parser)?;
    let tree = fromstring.call((crate::fix_tables::repair_text(body),), Some(&options))?;
    let element = document.getattr(pyo3::intern!(py, "docx"))?
        .getattr(pyo3::intern!(py, "_element"))?;
    let replace = element.getattr(pyo3::intern!(py, "replace"))?;
    let old = document.getattr(pyo3::intern!(py, "docx"))?
        .getattr(pyo3::intern!(py, "_element"))?
        .getattr(pyo3::intern!(py, "body"))?;
    replace.call1((old, &tree))?;
    Ok(tree)
}
