//! Render header and footer parts on an exposed Python document graph.
use crate::pyglobals::{FrameGlobals, frame_global};
use pyo3::basic::CompareOp;
use pyo3::prelude::*;

#[pyfunction]
pub(crate) fn render_live_headers(document: &Bound<'_, PyAny>, render_xml: &Bound<'_, PyAny>,
    encoding: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = document.py();
    let scope = FrameGlobals::current(py)?;
    let header = document.getattr(pyo3::intern!(py, "HEADER_URI"))?;
    let footer = document.getattr(pyo3::intern!(py, "FOOTER_URI"))?;
    for uri in [header, footer] {
        let relationships = document.getattr(pyo3::intern!(py, "docx"))?
            .getattr(pyo3::intern!(py, "_part"))?
            .getattr(pyo3::intern!(py, "rels"))?
            .call_method0(pyo3::intern!(py, "values"))?.try_iter()?;
        for relationship in relationships {
            let relationship = relationship?;
            let kind = relationship.getattr(pyo3::intern!(py, "reltype"))?;
            if kind.rich_compare(&uri, CompareOp::Ne)?.is_truthy()? { continue; }
            let target = relationship.getattr(pyo3::intern!(py, "target_part"))?;
            if !target.getattr(pyo3::intern!(py, "blob"))?.is_truthy()? { continue; }
            let old = relationship.getattr(pyo3::intern!(py, "target_part"))?;
            let serialize = document.getattr(pyo3::intern!(py, "xml_to_string"))?;
            let parse = frame_global(&scope, pyo3::intern!(py, "parse_xml"))?;
            let blob = old.getattr(pyo3::intern!(py, "blob"))?;
            let source = serialize.call1((parse.call1((blob,))?,))?;
            let rendered = render_xml.call1((source, &old))?;
            let xml = rendered.call_method1(pyo3::intern!(py, "encode"), (encoding,))?;
            let class = frame_global(&scope, pyo3::intern!(py, "XmlPart"))?;
            let load = class.getattr(pyo3::intern!(py, "load"))?;
            let name = old.getattr(pyo3::intern!(py, "partname"))?;
            let content_type = old.getattr(pyo3::intern!(py, "content_type"))?;
            let package = old.getattr(pyo3::intern!(py, "package"))?;
            let new_part = load.call1((name, content_type, xml, package))?;
            let relations = old.getattr(pyo3::intern!(py, "rels"))?
                .call_method0(pyo3::intern!(py, "values"))?.try_iter()?;
            for rel in relations {
                let rel = rel?;
                let load_rel = new_part.getattr(pyo3::intern!(py, "load_rel"))?;
                let kind = rel.getattr(pyo3::intern!(py, "reltype"))?;
                let target = rel.getattr(pyo3::intern!(py, "_target"))?;
                let id = rel.getattr(pyo3::intern!(py, "rId"))?;
                let external = rel.getattr(pyo3::intern!(py, "is_external"))?;
                load_rel.call1((kind, target, id, external))?;
            }
            relationship.setattr(pyo3::intern!(py, "_target"), new_part)?;
        }
    }
    Ok(())
}
