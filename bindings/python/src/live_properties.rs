//! Render properties on an exposed Python document using the existing session.
use crate::pyglobals::{FrameGlobals, frame_global};
use pyo3::prelude::*;

#[pyfunction]
pub(crate) fn render_live_properties(document: &Bound<'_, PyAny>, session: &Bound<'_, PyAny>,
    source_name: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = document.py();
    let scope = FrameGlobals::current(py)?;
    for name in [pyo3::intern!(py, "author"), pyo3::intern!(py, "comments"),
        pyo3::intern!(py, "identifier"), pyo3::intern!(py, "language"),
        pyo3::intern!(py, "subject"), pyo3::intern!(py, "title")] {
        let properties = document.getattr(pyo3::intern!(py, "docx"))?
            .getattr(pyo3::intern!(py, "core_properties"))?;
        let getter = frame_global(&scope, pyo3::intern!(py, "getattr"))?;
        let initial = getter.call1((&properties, name))?;
        let rendered = session.call_method1(pyo3::intern!(py, "render"), (source_name, initial))?;
        let setter = frame_global(&scope, pyo3::intern!(py, "setattr"))?;
        setter.call1((&properties, name, rendered))?;
    }
    Ok(())
}
