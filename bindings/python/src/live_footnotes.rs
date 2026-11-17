//! Render footnote parts on an exposed Python document graph.
use crate::pyglobals::{FrameGlobals, frame_global};
use pyo3::basic::CompareOp;
use pyo3::prelude::*;

#[pyfunction]
pub(crate) fn render_live_footnotes(document: &Bound<'_, PyAny>, render_xml: &Bound<'_, PyAny>,
    note_type: &Bound<'_, PyAny>, encoding: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = document.py();
    let scope = FrameGlobals::current(py)?;
    let sections = document.getattr(pyo3::intern!(py, "docx"))?
        .getattr(pyo3::intern!(py, "sections"))?.try_iter()?;
    for section in sections {
        let section = section?;
        let parts = section.getattr(pyo3::intern!(py, "part"))?
            .getattr(pyo3::intern!(py, "package"))?
            .getattr(pyo3::intern!(py, "parts"))?.try_iter()?;
        for part in parts {
            let part = part?;
            if !part.getattr(pyo3::intern!(py, "content_type"))?
                .rich_compare(note_type, CompareOp::Eq)?.is_truthy()? { continue; }
            let check = frame_global(&scope, pyo3::intern!(py, "isinstance"))?;
            let first_blob = part.getattr(pyo3::intern!(py, "blob"))?;
            let bytes = frame_global(&scope, pyo3::intern!(py, "bytes"))?;
            let is_bytes = check.call1((first_blob, bytes))?.is_truthy()?;
            let blob = part.getattr(pyo3::intern!(py, "blob"))?;
            let source = if is_bytes {
                blob.call_method1(pyo3::intern!(py, "decode"), (encoding,))?
            } else { blob };
            let rendered = render_xml.call1((source, &part))?;
            let bytes = rendered.call_method1(pyo3::intern!(py, "encode"), (encoding,))?;
            part.setattr(pyo3::intern!(py, "_blob"), bytes)?;
        }
    }
    Ok(())
}
