//! Picture replacement on the caller-visible document graph.
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

fn namespaces<'py>(py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
    super::docx_module(py)?.bind(py).getattr(pyo3::intern!(py, "oxml"))?.getattr(pyo3::intern!(py, "ns"))?.getattr(pyo3::intern!(py, "nsmap"))
}
fn xpath<'py>(node: &Bound<'py, PyAny>, expression: &str) -> PyResult<Bound<'py, PyAny>> {
    let py = node.py();
    let options = PyDict::new(py);
    options.set_item("namespaces", namespaces(node.py())?)?;
    node.getattr(pyo3::intern!(py, "xpath"))?.call((expression,), Some(&options))
}

fn graphic<'py>(tpl: &Bound<'py, PyAny>, part: &Bound<'py, PyAny>, graphic: &Bound<'py, PyAny>, part_map: &Bound<'py, PyDict>, replaced: &Bound<'py, PyAny>) -> PyResult<()> {
    let py = tpl.py();
    let uri = graphic.getattr(pyo3::intern!(py, "attrib"))?.get_item("uri")?;
    if !uri.eq(namespaces(py)?.get_item("pic")?)? { return Ok(()); }
    let blip = xpath(graphic, "pic:pic/pic:blipFill/a:blip")?.get_item(0)?;
    let dest = xpath(&blip, "@r:embed")?;
    if dest.len()? == 0 { return Ok(()); }
    let rel = dest.get_item(0)?;
    let filename = xpath(graphic, "pic:pic/pic:nvPicPr/pic:cNvPr/@name")?.get_item(0)?;
    let titles = xpath(graphic, "pic:pic/pic:nvPicPr/pic:cNvPr/@title")?;
    let title = if titles.is_truthy()? { titles.get_item(0)? } else { "".into_pyobject(py)?.into_any() };
    let descriptions = xpath(graphic, "pic:pic/pic:nvPicPr/pic:cNvPr/@descr")?;
    let description = if descriptions.is_truthy()? { descriptions.get_item(0)? } else { "".into_pyobject(py)?.into_any() };
    let target_ref = part.getattr(pyo3::intern!(py, "rels"))?.get_item(&rel)?.getattr(pyo3::intern!(py, "target_ref"))?;
    let target_part = part.getattr(pyo3::intern!(py, "rels"))?.get_item(&rel)?.getattr(pyo3::intern!(py, "target_part"))?;
    part_map.set_item(&filename, PyTuple::new(py, [target_ref, target_part])?)?;
    for item in tpl.getattr(pyo3::intern!(py, "pics_to_replace"))?.call_method0(pyo3::intern!(py, "items"))?.try_iter()? {
        let (id, data) = super::unpack_two(&item?)?;
        let id = id.bind(py);
        if id.eq(&filename)? || id.eq(&title)? || id.eq(&description)? {
            let entry = part_map.get_item(&filename)?.ok_or_else(||
                pyo3::exceptions::PyKeyError::new_err(filename.clone().unbind()))?;
            entry.get_item(1)?.setattr(pyo3::intern!(py, "_blob"), data.bind(py))?;
            replaced.set_item(id, true)?;
            break;
        }
    }
    Ok(())
}

#[pyfunction]
pub(crate) fn replace_part_pics(tpl: &Bound<'_, PyAny>, part: &Bound<'_, PyAny>, replaced: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = tpl.py();
    let etree = super::LXML_ETREE_MODULE.get_or_try_init(py, || {
        super::find_loaded_module(py, pyo3::intern!(py, "lxml.etree"))?
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("lxml is required for the exposed document model"))
    })?.bind(py);
    let tree = etree.call_method1(pyo3::intern!(py, "fromstring"), (part.getattr(pyo3::intern!(py, "blob"))?,))?;
    let part_map = PyDict::new(py);
    for item in xpath(&tree, "//a:graphic/a:graphicData")?.try_iter()? {
        if let Err(error) = graphic(tpl, part, &item?, &part_map, replaced) {
            if !error.is_instance_of::<pyo3::exceptions::PyException>(py) { return Err(error); }
        }
    }
    tpl.getattr(pyo3::intern!(py, "pic_map"))?.call_method1(pyo3::intern!(py, "update"), (part_map,))?;
    Ok(())
}

#[pyfunction]
pub(crate) fn replace_pics(tpl: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = tpl.py();
    let replaced = PyDict::new(py);
    for key in tpl.getattr(pyo3::intern!(py, "pics_to_replace"))?.try_iter()? { replaced.set_item(key?, false)?; }
    let part = tpl.getattr(pyo3::intern!(py, "docx"))?.getattr(pyo3::intern!(py, "part"))?;
    tpl.call_method1(pyo3::intern!(py, "_replace_docx_part_pics"), (&part, &replaced))?;
    let constants = super::find_loaded_module(py, pyo3::intern!(py, "docx.opc.constants"))?
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("docx constants are required"))?;
    let relationship_types = constants.bind(py).getattr(pyo3::intern!(py, "RELATIONSHIP_TYPE"))?;
    for item in part.getattr(pyo3::intern!(py, "rels"))?.call_method0(pyo3::intern!(py, "items"))?.try_iter()? {
        let (_, relation) = super::unpack_two(&item?)?;
        let relation = relation.bind(py);
        let kind = relation.getattr(pyo3::intern!(py, "reltype"))?;
        if kind.eq(relationship_types.getattr(pyo3::intern!(py, "HEADER"))?)? || kind.eq(relationship_types.getattr(pyo3::intern!(py, "FOOTER"))?)? {
            tpl.call_method1(pyo3::intern!(py, "_replace_docx_part_pics"), (relation.getattr(pyo3::intern!(py, "target_part"))?, &replaced))?;
        }
    }
    if !tpl.getattr(pyo3::intern!(py, "allow_missing_pics"))?.is_truthy()? {
        for (id, did_replace) in replaced.iter() {
            if !did_replace.is_truthy()? {
                return Err(pyo3::exceptions::PyValueError::new_err(format!("Picture {} not found in the docx template", id.str()?)));
            }
        }
    }
    Ok(())
}

#[pyfunction]
pub(crate) fn pre_process(tpl: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = tpl.py();
    if tpl.getattr(pyo3::intern!(py, "pics_to_replace"))?.is_truthy()? { tpl.call_method0(pyo3::intern!(py, "_replace_pics"))?; }
    Ok(())
}
