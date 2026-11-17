//! Diagram relationships on the caller-visible document graph.
use crate::unpack_pair;
use std::collections::HashSet;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFunction, PyList, PyModule, PySet, PyString, PyStringData, PyTuple};
use super::pyglobals::{global,frame_global,FrameGlobals};

struct ComposerDependencies {
    utils: Py<PyAny>,
    constants: Py<PyAny>,
    composer: Option<Py<PyAny>>,
}

static DEPENDENCIES: pyo3::sync::PyOnceLock<ComposerDependencies> = pyo3::sync::PyOnceLock::new();

fn loaded_dependencies(py: Python<'_>) -> PyResult<Option<ComposerDependencies>> {
    let Some(utils) = super::find_loaded_module(py, pyo3::intern!(py,"docxcompose.utils"))? else { return Ok(None); };
    let Some(constants) = super::find_loaded_module(py, pyo3::intern!(py,"docx.opc.constants"))? else { return Ok(None); };
    let composer = super::find_loaded_module(py, pyo3::intern!(py,"docxcompose.composer"))?;
    Ok(Some(ComposerDependencies { utils, constants, composer }))
}

pub(crate) fn capture_loaded_dependencies(py: Python<'_>) -> PyResult<()> {
    if DEPENDENCIES.get(py).is_none() {
        if let Some(dependencies) = loaded_dependencies(py)? { let _ = DEPENDENCIES.set(py, dependencies); }
    }
    Ok(())
}

fn dependencies(py: Python<'_>) -> PyResult<&'static ComposerDependencies> {
    DEPENDENCIES.get_or_try_init(py, || loaded_dependencies(py)?
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("document model dependencies are not loaded")))
}

#[pyfunction]
pub(crate) fn composer_constants(py: Python<'_>) -> PyResult<(Py<PyAny>,Py<PyAny>)> {
    let deps=dependencies(py)?;
    Ok((deps.utils.bind(py).getattr(pyo3::intern!(py,"NS"))?.unbind(),
        deps.constants.bind(py).getattr(pyo3::intern!(py,"RELATIONSHIP_TYPE"))?.unbind()))
}

fn native_xpath<'py>(element: &Bound<'py, PyAny>, query: &Bound<'py, PyString>, deps: &ComposerDependencies) -> PyResult<Bound<'py, PyAny>> {
    let py=element.py();
    let utils=deps.utils.bind(py);
    let base=utils.getattr(pyo3::intern!(py,"BaseOxmlElement"))?;
    let is_base=element.is_instance(&base)?;
    let xpath=element.getattr(pyo3::intern!(py,"xpath"))?;
    if is_base { xpath.call1((query,)) } else {
        let options=PyDict::new(py);
        options.set_item(pyo3::intern!(py,"namespaces"),utils.getattr(pyo3::intern!(py,"NS"))?)?;
        xpath.call((query,),Some(&options))
    }
}

fn composer_scope<'py>(py: Python<'py>, deps: &ComposerDependencies) -> PyResult<Bound<'py, PyDict>> {
    if let Some(composer)=deps.composer.as_ref() {
        return Ok(composer.bind(py).cast::<PyModule>()?.dict());
    }
    // The dependencies were captured before docxcompose.composer was loaded.
    // A class retains its defining globals after its module is removed, so
    // read them from the Composer class the package subdoc module imported.
    let subdoc=super::find_loaded_module(py,pyo3::intern!(py,"docxtpl_native.subdoc"))?
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("composer globals are unavailable"))?;
    let function=subdoc.bind(py).getattr(pyo3::intern!(py,"Composer"))?
        .getattr(pyo3::intern!(py,"remove_header_and_footer_references"))?;
    Ok(function.getattr(pyo3::intern!(py,"__globals__"))?.cast_into::<PyDict>()?)
}

fn composer_xpath<'py>(xpath: &Bound<'py, PyAny>, element: &Bound<'py, PyAny>, query: &Bound<'py, PyString>, deps: &ComposerDependencies) -> PyResult<Bound<'py, PyAny>> {
    let py=element.py();
    let original=xpath.is_instance_of::<PyFunction>()
        && xpath.getattr(pyo3::intern!(py,"__globals__"))?
            .is(&deps.utils.bind(py).cast::<PyModule>()?.dict())
        && xpath.getattr(pyo3::intern!(py,"__code__"))?.getattr(pyo3::intern!(py,"co_name"))?.eq("xpath")?;
    if original { native_xpath(element,query,deps) } else { xpath.call1((element,query)) }
}

#[pyfunction]
pub(crate) fn init_programmatic_subdoc(subdoc: &Bound<'_, PyAny>, tpl: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = subdoc.py();
    let scope = FrameGlobals::current(py)?;
    subdoc.setattr(pyo3::intern!(py, "tpl"), tpl)?;
    subdoc.setattr(pyo3::intern!(py, "docx"), tpl.call_method0(pyo3::intern!(py, "get_docx"))?)?;
    let document = frame_global(&scope, pyo3::intern!(py, "Document"))?.call1((py.None(),))?;
    subdoc.setattr(pyo3::intern!(py, "subdocx"), &document)?;
    document.setattr(pyo3::intern!(py, "_part"), subdoc.getattr(pyo3::intern!(py, "docx"))?.getattr(pyo3::intern!(py, "_part"))?)?;
    Ok(())
}

// Match the reference's `</?w:body[^>]*>` substitution on Python Unicode
// code points, including isolated surrogates that cannot be represented in str.
fn strip_body_tags<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    let text = match value.cast::<PyString>() {
        Ok(text) => text,
        Err(_) => {
            let mut view = std::mem::MaybeUninit::<pyo3::ffi::Py_buffer>::uninit();
            let has_buffer = unsafe { pyo3::ffi::PyObject_GetBuffer(value.as_ptr(), view.as_mut_ptr(), pyo3::ffi::PyBUF_SIMPLE) } == 0;
            if has_buffer {
                unsafe { pyo3::ffi::PyBuffer_Release(view.as_mut_ptr()) };
            } else {
                unsafe { pyo3::ffi::PyErr_Clear() };
            }
            let message = if has_buffer {
                "cannot use a string pattern on a bytes-like object".to_owned()
            } else {
                let name: String = value.get_type().getattr(pyo3::intern!(py, "__name__"))?.extract()?;
                format!("expected string or bytes-like object, got '{name}'")
            };
            return Err(pyo3::exceptions::PyTypeError::new_err(message));
        }
    };
    // The Python string remains borrowed and immutable while its canonical
    // storage is inspected under the GIL. PyO3 readies it on Python 3.11.
    let out = match unsafe { text.data()? } {
        PyStringData::Ucs1(chars) => remove_body_tags(chars),
        PyStringData::Ucs2(chars) => remove_body_tags(chars),
        PyStringData::Ucs4(chars) => remove_body_tags(chars),
    };
    if text.is_exact_instance_of::<PyString>() && out.len() == text.len()? {
        return Ok(value.clone());
    }
    let result = unsafe { pyo3::ffi::PyUnicode_FromKindAndData(4, out.as_ptr().cast(), out.len() as isize) };
    unsafe { Bound::from_owned_ptr_or_err(py, result) }
}

fn remove_body_tags<T: Copy + Into<u32>>(chars: &[T]) -> Vec<u32> {
    let mut out = Vec::with_capacity(chars.len());
    let mut index = 0;
    while index < chars.len() {
        let mut start = index;
        if chars[start].into() == b'<' as u32 {
            start += 1;
            if chars.get(start).is_some_and(|ch| (*ch).into() == b'/' as u32) { start += 1; }
            if let Some(prefix) = chars.get(start..start + 6) {
                if prefix.iter().zip(b"w:body").all(|(ch, expected)| (*ch).into() == *expected as u32) {
                    if let Some(end) = chars[start + 6..].iter().position(|ch| (*ch).into() == b'>' as u32) {
                        index = start + 6 + end + 1;
                        continue;
                    }
                }
            }
        }
        out.push(chars[index].into());
        index += 1;
    }
    out
}

#[pyfunction]
pub(crate) fn init_file_subdoc(subdoc: &Bound<'_, PyAny>, tpl: &Bound<'_, PyAny>, source: &Bound<'_, PyAny>) -> PyResult<()> {
    let scope = FrameGlobals::current(subdoc.py())?;
    init_file_graph(subdoc, tpl, source, &scope)
}

fn init_file_graph(subdoc: &Bound<'_, PyAny>, tpl: &Bound<'_, PyAny>, source: &Bound<'_, PyAny>, scope: &FrameGlobals<'_>) -> PyResult<()> {
    let py = subdoc.py();
    let master = tpl.call_method0(pyo3::intern!(py, "get_docx"))?;
    subdoc.setattr(pyo3::intern!(py, "docx"), &master)?;
    let child = frame_global(scope, pyo3::intern!(py, "Document"))?.call1((source,))?;
    subdoc.setattr(pyo3::intern!(py, "subdocx"), &child)?;
    if source.is_truthy()? {
        let composer = frame_global(scope, pyo3::intern!(py, "SubdocComposer"))?.call1((&master,))?;
        composer.call_method1(pyo3::intern!(py, "attach_parts"), (&child,))?;
    } else {
        child.setattr(pyo3::intern!(py, "_part"), master.getattr(pyo3::intern!(py, "_part"))?)?;
    }
    Ok(())
}

#[pyfunction]
#[pyo3(signature = (subdoc, unsupported_class=None))]
pub(crate) fn programmatic_subdoc_xml<'py>(subdoc: &Bound<'py, PyAny>, unsupported_class: Option<&Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyAny>> {
    let py = subdoc.py();
    let scope = FrameGlobals::current(py)?;
    if let Some(class) = unsupported_class {
        if !super::live_routing::has_subdoc_graph(subdoc)? {
            let tpl = subdoc.getattr(pyo3::intern!(py, "tpl"))?;
            if tpl.is_none() {
                return Err(PyErr::from_value(class.call1(("file composition into an exposed Python model needs the native graph adapter",))?));
            }
            let source = subdoc.getattr(pyo3::intern!(py, "source"))?;
            init_file_graph(subdoc, &tpl, &source, &scope)?;
        }
    }
    let body = || subdoc.getattr(pyo3::intern!(py, "subdocx"))?.getattr(pyo3::intern!(py, "element"))?.getattr(pyo3::intern!(py, "body"));
    if !body()?.getattr(pyo3::intern!(py, "sectPr"))?.is_none() {
        let remove = body()?.getattr(pyo3::intern!(py, "remove"))?;
        let section = body()?.getattr(pyo3::intern!(py, "sectPr"))?;
        remove.call1((section,))?;
    }
    let options = PyDict::new(py);
    options.set_item(pyo3::intern!(py, "encoding"), pyo3::intern!(py, "unicode"))?;
    options.set_item(pyo3::intern!(py, "pretty_print"), false)?;
    let etree = frame_global(&scope, pyo3::intern!(py, "etree"))?;
    let tostring = etree.getattr(pyo3::intern!(py, "tostring"))?;
    let xml = tostring.call((body()?,), Some(&options))?;
    strip_body_tags(&xml)
}

#[pyfunction]
pub(crate) fn reset_composer(tpl: &Bound<'_, PyAny>) -> PyResult<()> {
    let py=tpl.py();
    tpl.setattr(pyo3::intern!(py,"num_id_mapping"),PyDict::new(py))?;
    tpl.setattr(pyo3::intern!(py,"anum_id_mapping"),PyDict::new(py))?;
    tpl.setattr(pyo3::intern!(py,"_numbering_restarted"),PySet::empty(py)?)?;
    Ok(())
}

#[pyfunction]
pub(crate) fn init_composer(tpl: &Bound<'_, PyAny>, doc: &Bound<'_, PyAny>) -> PyResult<()> {
    let py=tpl.py();
    tpl.setattr(pyo3::intern!(py,"doc"),doc)?;
    let package=doc.getattr(pyo3::intern!(py,"part"))?.getattr(pyo3::intern!(py,"package"))?;
    tpl.setattr(pyo3::intern!(py,"pkg"),package)?;
    tpl.setattr(pyo3::intern!(py,"restart_numbering"),true)?;
    tpl.call_method0(pyo3::intern!(py,"reset_reference_mapping"))?;
    tpl.setattr(pyo3::intern!(py,"first_section_properties_added"),false)?;
    Ok(())
}

#[pyfunction]
pub(crate) fn composer_attach_parts(tpl: &Bound<'_, PyAny>, doc: &Bound<'_, PyAny>, remove_property_fields: &Bound<'_, PyAny>) -> PyResult<()> {
    let py=tpl.py();
    let scope=FrameGlobals::current(py)?;
    tpl.call_method0(pyo3::intern!(py,"reset_reference_mapping"))?;
    if remove_property_fields.is_truthy()? {
        let class=frame_global(&scope,pyo3::intern!(py,"CustomProperties"))?;
        let properties=class.call1((doc,))?;
        let names=properties.call_method0(pyo3::intern!(py,"keys"))?;
        for name in names.try_iter()? {
            properties.call_method1(pyo3::intern!(py,"dissolve_fields"),(name?,))?;
        }
    }
    tpl.call_method1(pyo3::intern!(py,"_create_style_id_mapping"),(doc,))?;
    let body=doc.getattr(pyo3::intern!(py,"element"))?.getattr(pyo3::intern!(py,"body"))?;
    for element in body.try_iter()? {
        let element=element?;
        let isinstance=frame_global(&scope,pyo3::intern!(py,"isinstance"))?;
        let section=frame_global(&scope,pyo3::intern!(py,"CT_SectPr"))?;
        if isinstance.call1((&element,section))?.is_truthy()? { continue; }
        let add=tpl.getattr(pyo3::intern!(py,"add_referenced_parts"))?;
        let source=doc.getattr(pyo3::intern!(py,"part"))?;
        let target=tpl.getattr(pyo3::intern!(py,"doc"))?.getattr(pyo3::intern!(py,"part"))?;
        add.call1((source,target,&element))?;
        for method in [pyo3::intern!(py,"add_styles"),pyo3::intern!(py,"add_numberings"),pyo3::intern!(py,"restart_first_numbering"),pyo3::intern!(py,"add_images"),pyo3::intern!(py,"add_diagrams"),pyo3::intern!(py,"add_shapes"),pyo3::intern!(py,"add_footnotes"),pyo3::intern!(py,"remove_header_and_footer_references")] {
            tpl.call_method1(method,(doc,&element))?;
        }
    }
    tpl.call_method1(pyo3::intern!(py,"add_styles_from_other_parts"),(doc,))?;
    tpl.call_method0(pyo3::intern!(py,"renumber_bookmarks"))?;
    tpl.call_method0(pyo3::intern!(py,"renumber_docpr_ids"))?;
    tpl.call_method0(pyo3::intern!(py,"renumber_nvpicpr_ids"))?;
    tpl.call_method1(pyo3::intern!(py,"fix_section_types"),(doc,))?;
    Ok(())
}

#[pyfunction]
pub(crate) fn composer_fix_section_types(tpl: &Bound<'_, PyAny>, doc: &Bound<'_, PyAny>) -> PyResult<()> {
    let py=tpl.py();
    let doc_attr=pyo3::intern!(py,"doc");
    let sections=pyo3::intern!(py,"sections");
    let start_type=pyo3::intern!(py,"start_type");
    if tpl.getattr(doc_attr)?.getattr(sections)?.len()? == 1 || doc.getattr(sections)?.len()? == 1 { return Ok(()); }
    let index=tpl.getattr(doc_attr)?.getattr(sections)?.len()? as isize - doc.getattr(sections)?.len()? as isize;
    let last=tpl.getattr(doc_attr)?.getattr(sections)?.get_item(-1)?.getattr(start_type)?;
    tpl.getattr(doc_attr)?.getattr(sections)?.get_item(index)?.setattr(start_type,last)?;
    let last=doc.getattr(sections)?.get_item(-1)?.getattr(start_type)?;
    tpl.getattr(doc_attr)?.getattr(sections)?.get_item(-1)?.setattr(start_type,last)?;
    Ok(())
}

#[pyfunction]
pub(crate) fn composer_remove_header_and_footer_references(element: &Bound<'_, PyAny>, query: &Bound<'_, PyString>) -> PyResult<()> {
    let py=element.py();
    let deps=dependencies(py)?;
    let scope=composer_scope(py,deps)?;
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    let refs=composer_xpath(&xpath,element,query,deps)?;
    for reference in refs.try_iter()? {
        let reference=reference?;
        reference.call_method0(pyo3::intern!(py,"getparent"))?
            .call_method1(pyo3::intern!(py,"remove"),(reference,))?;
    }
    Ok(())
}

#[pyfunction]
pub(crate) fn composer_renumber_bookmarks(tpl: &Bound<'_, PyAny>, start_query: &Bound<'_, PyString>, end_query: &Bound<'_, PyString>) -> PyResult<()> {
    let py=tpl.py();
    let deps=dependencies(py)?;
    let scope=composer_scope(py,deps)?;
    for query in [start_query,end_query] {
        let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
        let body=tpl.getattr(pyo3::intern!(py,"doc"))?.getattr(pyo3::intern!(py,"element"))?.getattr(pyo3::intern!(py,"body"))?;
        let bookmarks=composer_xpath(&xpath,&body,query,deps)?;
        for (id,bookmark) in bookmarks.try_iter()?.enumerate() {
            let bookmark=bookmark?;
            let setter=bookmark.getattr(pyo3::intern!(py,"set"))?;
            let ns=global(&scope,pyo3::intern!(py,"NS"))?;
            let name=PyString::new(py,"{%s}id").call_method1(pyo3::intern!(py,"__mod__"),(ns.get_item(pyo3::intern!(py,"w"))?,))?;
            let stringify=global(&scope,pyo3::intern!(py,"str"))?;
            let value=stringify.call1((id,))?;
            setter.call1((name,value))?;
        }
    }
    Ok(())
}

#[pyfunction]
pub(crate) fn composer_renumber_drawing_ids(tpl: &Bound<'_, PyAny>, query: &Bound<'_, PyString>) -> PyResult<()> {
    let py=tpl.py();
    let deps=dependencies(py)?;
    let scope=composer_scope(py,deps)?;
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    let body=tpl.getattr(pyo3::intern!(py,"doc"))?.getattr(pyo3::intern!(py,"element"))?.getattr(pyo3::intern!(py,"body"))?;
    let nodes=composer_xpath(&xpath,&body,query,deps)?;
    let mut id=1usize;
    for node in nodes.try_iter()? {
        node?.setattr(pyo3::intern!(py,"id"),id)?;
        id+=1;
    }
    let rels=tpl.getattr(pyo3::intern!(py,"doc"))?.getattr(pyo3::intern!(py,"part"))?
        .getattr(pyo3::intern!(py,"rels"))?.call_method0(pyo3::intern!(py,"values"))?;
    let mut parts=Vec::new();
    for rel in rels.try_iter()? {
        let rel=rel?;
        let kind=rel.getattr(pyo3::intern!(py,"reltype"))?;
        let header=global(&scope,pyo3::intern!(py,"RT"))?.getattr(pyo3::intern!(py,"HEADER"))?;
        let footer=global(&scope,pyo3::intern!(py,"RT"))?.getattr(pyo3::intern!(py,"FOOTER"))?;
        if PyList::new(py,[header,footer])?.contains(kind)? {
            parts.push(rel.getattr(pyo3::intern!(py,"target_part"))?);
        }
    }
    for part in parts {
        let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
        let element=part.getattr(pyo3::intern!(py,"element"))?;
        let nodes=composer_xpath(&xpath,&element,query,deps)?;
        for node in nodes.try_iter()? {
            node?.setattr(pyo3::intern!(py,"id"),id)?;
            id+=1;
        }
    }
    Ok(())
}

#[pyfunction]
pub(crate) fn composer_add_referenced_parts(tpl: &Bound<'_, PyAny>, src: &Bound<'_, PyAny>, dst: &Bound<'_, PyAny>, element: &Bound<'_, PyAny>, query: &Bound<'_, PyString>) -> PyResult<()> {
    let py=tpl.py();
    let deps=dependencies(py)?;
    let scope=composer_scope(py,deps)?;
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    let nodes=composer_xpath(&xpath,element,query,deps)?;
    for node in nodes.try_iter()? {
        let node=node?;
        let get=node.getattr(pyo3::intern!(py,"get"))?;
        let ns=global(&scope,pyo3::intern!(py,"NS"))?;
        let name=PyString::new(py,"{%s}id").call_method1(pyo3::intern!(py,"__mod__"),(ns.get_item(pyo3::intern!(py,"r"))?,))?;
        let rid=get.call1((name,))?;
        let rel=src.getattr(pyo3::intern!(py,"rels"))?.get_item(rid)?;
        let kind=rel.getattr(pyo3::intern!(py,"reltype"))?;
        let ignored=global(&scope,pyo3::intern!(py,"REFERENCED_PARTS_IGNORED_RELTYPES"))?;
        if ignored.contains(kind)? { continue; }
        let new_rel=tpl.call_method1(pyo3::intern!(py,"add_relationship"),(src,dst,rel))?;
        let set=node.getattr(pyo3::intern!(py,"set"))?;
        let ns=global(&scope,pyo3::intern!(py,"NS"))?;
        let name=PyString::new(py,"{%s}id").call_method1(pyo3::intern!(py,"__mod__"),(ns.get_item(pyo3::intern!(py,"r"))?,))?;
        let new_id=new_rel.getattr(pyo3::intern!(py,"rId"))?;
        set.call1((name,new_id))?;
    }
    Ok(())
}

fn add_media(tpl: &Bound<'_, PyAny>, doc: &Bound<'_, PyAny>, element: &Bound<'_, PyAny>, query: &Bound<'_, PyString>, images: bool) -> PyResult<()> {
    let py=tpl.py();
    let deps=dependencies(py)?;
    let scope=composer_scope(py,deps)?;
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    let nodes=composer_xpath(&xpath,element,query,deps)?;
    for node in nodes.try_iter()? {
        let node=node?;
        let get=node.getattr(pyo3::intern!(py,"get"))?;
        let ns=global(&scope,pyo3::intern!(py,"NS"))?;
        let suffix=if images { "embed" } else { "id" };
        let name=PyString::new(py,&format!("{{%s}}{suffix}")).call_method1(pyo3::intern!(py,"__mod__"),(ns.get_item(pyo3::intern!(py,"r"))?,))?;
        let rid=get.call1((name,))?;
        let part=doc.getattr(pyo3::intern!(py,"part"))?.getattr(pyo3::intern!(py,"rels"))?.get_item(rid)?.getattr(pyo3::intern!(py,"target_part"))?;
        let parts=tpl.getattr(pyo3::intern!(py,"pkg"))?.getattr(pyo3::intern!(py,"image_parts"))?;
        let find=parts.getattr(pyo3::intern!(py,"_get_by_sha1"))?;
        let mut image=find.call1((part.getattr(pyo3::intern!(py,"sha1"))?,))?;
        if image.is_none() {
            let wrapper=global(&scope,pyo3::intern!(py,"ImageWrapper"))?.call1((&part,))?;
            image=tpl.getattr(pyo3::intern!(py,"pkg"))?.getattr(pyo3::intern!(py,"image_parts"))?.call_method1(pyo3::intern!(py,"_add_image_part"),(wrapper,))?;
        }
        let destination=tpl.getattr(pyo3::intern!(py,"doc"))?.getattr(pyo3::intern!(py,"part"))?;
        let relate=destination.getattr(pyo3::intern!(py,"relate_to"))?;
        let kind=global(&scope,pyo3::intern!(py,"RT"))?.getattr(pyo3::intern!(py,"IMAGE"))?;
        let new_id=relate.call1((image,kind))?;
        let set=node.getattr(pyo3::intern!(py,"set"))?;
        let ns=global(&scope,pyo3::intern!(py,"NS"))?;
        let name=PyString::new(py,&format!("{{%s}}{suffix}")).call_method1(pyo3::intern!(py,"__mod__"),(ns.get_item(pyo3::intern!(py,"r"))?,))?;
        set.call1((name,new_id))?;
        if images {
            let get=node.getattr(pyo3::intern!(py,"get"))?;
            let ns=global(&scope,pyo3::intern!(py,"NS"))?;
            let name=PyString::new(py,"{%s}link").call_method1(pyo3::intern!(py,"__mod__"),(ns.get_item(pyo3::intern!(py,"r"))?,))?;
            let rid=get.call1((name,))?;
            if rid.is_truthy()? {
                let rel=doc.getattr(pyo3::intern!(py,"part"))?.getattr(pyo3::intern!(py,"rels"))?.get_item(rid)?;
                let add=tpl.getattr(pyo3::intern!(py,"add_relationship"))?;
                let destination=tpl.getattr(pyo3::intern!(py,"doc"))?.getattr(pyo3::intern!(py,"part"))?;
                let rel=add.call1((py.None(),destination,rel))?;
                let set=node.getattr(pyo3::intern!(py,"set"))?;
                let ns=global(&scope,pyo3::intern!(py,"NS"))?;
                let name=PyString::new(py,"{%s}link").call_method1(pyo3::intern!(py,"__mod__"),(ns.get_item(pyo3::intern!(py,"r"))?,))?;
                set.call1((name,rel.getattr(pyo3::intern!(py,"rId"))?))?;
            }
        }
    }
    Ok(())
}

#[pyfunction]
pub(crate) fn composer_add_images(tpl: &Bound<'_, PyAny>, doc: &Bound<'_, PyAny>, element: &Bound<'_, PyAny>, query: &Bound<'_, PyString>) -> PyResult<()> {
    add_media(tpl,doc,element,query,true)
}

#[pyfunction]
pub(crate) fn composer_add_shapes(tpl: &Bound<'_, PyAny>, doc: &Bound<'_, PyAny>, element: &Bound<'_, PyAny>, query: &Bound<'_, PyString>) -> PyResult<()> {
    add_media(tpl,doc,element,query,false)
}

#[pyfunction]
pub(crate) fn composer_add_styles_from_other_parts(tpl: &Bound<'_, PyAny>, doc: &Bound<'_, PyAny>) -> PyResult<()> {
    let py=tpl.py();
    let deps=dependencies(py)?;
    let scope=composer_scope(py,deps)?;
    let kinds=global(&scope,pyo3::intern!(py,"PART_RELTYPES_WITH_STYLES"))?;
    for kind in kinds.try_iter()? {
        let kind=kind?;
        let element=(|| -> PyResult<Bound<'_, PyAny>> {
            let parse=global(&scope,pyo3::intern!(py,"parse_xml"))?;
            let rels=doc.getattr(pyo3::intern!(py,"part"))?.getattr(pyo3::intern!(py,"rels"))?;
            let part=rels.call_method1(pyo3::intern!(py,"part_with_reltype"),(kind,))?;
            parse.call1((part.getattr(pyo3::intern!(py,"blob"))?,))
        })();
        match element {
            Ok(element) => { tpl.call_method1(pyo3::intern!(py,"add_styles"),(doc,element))?; }
            Err(error) => {
                let chain=|new: PyErr| {
                    if new.value(py).as_ptr()!=error.value(py).as_ptr() {
                        unsafe {
                            pyo3::ffi::Py_INCREF(error.value(py).as_ptr());
                            pyo3::ffi::PyException_SetContext(new.value(py).as_ptr(),error.value(py).as_ptr());
                        }
                    }
                    new
                };
                let key=global(&scope,pyo3::intern!(py,"KeyError")).map_err(&chain)?;
                let value=global(&scope,pyo3::intern!(py,"ValueError")).map_err(&chain)?;
                if unsafe { pyo3::ffi::PyExceptionClass_Check(key.as_ptr()) == 0 || pyo3::ffi::PyExceptionClass_Check(value.as_ptr()) == 0 } {
                    return Err(chain(pyo3::exceptions::PyTypeError::new_err("catching classes that do not inherit from BaseException is not allowed")));
                }
                let classes=PyTuple::new(py,[key,value])?;
                if !error.is_instance(py,classes.as_any()) { return Err(error); }
            }
        }
    }
    Ok(())
}

#[pyfunction]
pub(crate) fn composer_insert_numbering(tpl: &Bound<'_, PyAny>, element: &Bound<'_, PyAny>, query: &Bound<'_, PyString>, abstract_num: bool) -> PyResult<()> {
    let py=tpl.py();
    let part=tpl.call_method0(pyo3::intern!(py,"numbering_part"))?;
    let root=part.getattr(pyo3::intern!(py,"element"))?;
    let nums=root.call_method1(pyo3::intern!(py,"xpath"),(query,))?;
    if nums.is_truthy()? {
        let root=part.getattr(pyo3::intern!(py,"element"))?;
        let index=root.getattr(pyo3::intern!(py,"index"))?;
        let target=nums.get_item(if abstract_num { 0 } else { -1 })?;
        let position=index.call1((target,))?;
        let root=part.getattr(pyo3::intern!(py,"element"))?;
        root.call_method1(pyo3::intern!(py,"insert"),(position,element))?;
    } else if abstract_num {
        let root=part.getattr(pyo3::intern!(py,"element"))?;
        root.call_method1(pyo3::intern!(py,"insert"),(0,element))?;
    } else {
        let root=part.getattr(pyo3::intern!(py,"element"))?;
        root.call_method1(pyo3::intern!(py,"append"),(element,))?;
    }
    Ok(())
}


#[pyfunction]
pub(crate) fn composer_replace_mapped_num_id(tpl: &Bound<'_, PyAny>, old_id: &Bound<'_, PyAny>, new_id: &Bound<'_, PyAny>) -> PyResult<()> {
    let py=tpl.py();
    let items=tpl.getattr(pyo3::intern!(py,"num_id_mapping"))?.call_method0(pyo3::intern!(py,"items"))?;
    for pair in items.try_iter()? {
        let (key,value)=unpack_pair(&pair?)?;
        if value.eq(old_id)? {
            tpl.getattr(pyo3::intern!(py,"num_id_mapping"))?.set_item(key,new_id)?;
            break;
        }
    }
    Ok(())
}

fn document_part<'py>(tpl: &Bound<'py, PyAny>, parsed: bool, partname: &Bound<'py, PyString>, templates: &Bound<'py, PyString>, filename: &Bound<'py, PyString>, mode: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    let py=tpl.py();
    let relationship=if parsed { pyo3::intern!(py,"NUMBERING") } else { pyo3::intern!(py,"FOOTNOTES") };
    let content_type_name=if parsed { pyo3::intern!(py,"WML_NUMBERING") } else { pyo3::intern!(py,"WML_FOOTNOTES") };
    let class_name=if parsed { pyo3::intern!(py,"NumberingPart") } else { pyo3::intern!(py,"Part") };
    let deps=dependencies(py)?;
    let scope=composer_scope(py,deps)?;
    let existing=(|| -> PyResult<Bound<'py, PyAny>> {
        let rels=tpl.getattr(pyo3::intern!(py,"doc"))?.getattr(pyo3::intern!(py,"part"))?.getattr(pyo3::intern!(py,"rels"))?;
        let find=rels.getattr(pyo3::intern!(py,"part_with_reltype"))?;
        let kind=global(&scope,pyo3::intern!(py,"RT"))?.getattr(relationship)?;
        find.call1((kind,))
    })();
    match existing {
        Ok(part) => Ok(part),
        Err(error) => {
            let guard=super::pyglobals::HandledException::new(py,&error);
            let result=(|| -> PyResult<Bound<'py, PyAny>> {
                let exception=global(&scope,pyo3::intern!(py,"KeyError"))?;
                let valid=if let Ok(tuple)=exception.cast::<PyTuple>() {
                    tuple.iter().all(|value| unsafe { pyo3::ffi::PyExceptionClass_Check(value.as_ptr()) != 0 })
                } else { unsafe { pyo3::ffi::PyExceptionClass_Check(exception.as_ptr()) != 0 } };
                if !valid { return Err(pyo3::exceptions::PyTypeError::new_err("catching classes that do not inherit from BaseException is not allowed")); }
                if !error.is_instance(py,&exception) { return Err(error.clone_ref(py)); }
                let name=global(&scope,pyo3::intern!(py,"PackURI"))?.call1((partname,))?;
                let content_type=global(&scope,pyo3::intern!(py,"CT"))?.getattr(content_type_name)?;
                let join=global(&scope,pyo3::intern!(py,"os"))?.getattr(pyo3::intern!(py,"path"))?.getattr(pyo3::intern!(py,"join"))?;
                let dirname=global(&scope,pyo3::intern!(py,"os"))?.getattr(pyo3::intern!(py,"path"))?.getattr(pyo3::intern!(py,"dirname"))?;
                let directory=dirname.call1((global(&scope,pyo3::intern!(py,"__file__"))?,))?;
                let path=join.call1((directory,templates,filename))?;
                let open=global(&scope,pyo3::intern!(py,"open"))?;
                let manager=open.call1((path,mode))?;
                let bytes=super::crc_binding::read_context_manager(&manager)?;
                let unbound=|| pyo3::exceptions::PyUnboundLocalError::new_err("cannot access local variable 'xml_bytes' where it is not associated with a value");
                let (class,content)=if parsed {
                    let parse=global(&scope,pyo3::intern!(py,"parse_xml"))?;
                    let content=parse.call1((bytes.ok_or_else(unbound)?,))?;
                    (global(&scope,class_name)?,content)
                } else {
                    let class=global(&scope,class_name)?;
                    (class,bytes.ok_or_else(unbound)?)
                };
                let package=tpl.getattr(pyo3::intern!(py,"doc"))?.getattr(pyo3::intern!(py,"part"))?.getattr(pyo3::intern!(py,"package"))?;
                let part=class.call1((name,content_type,content,package))?;
                let relate=tpl.getattr(pyo3::intern!(py,"doc"))?.getattr(pyo3::intern!(py,"part"))?.getattr(pyo3::intern!(py,"relate_to"))?;
                let kind=global(&scope,pyo3::intern!(py,"RT"))?.getattr(relationship)?;
                relate.call1((&part,kind))?;
                Ok(part)
            })();
            drop(guard);
            result.map_err(|new| super::pyglobals::chain_context_if_missing(py,&error,new))
        }
    }
}

#[pyfunction]
pub(crate) fn composer_numbering_part<'py>(tpl: &Bound<'py, PyAny>, partname: &Bound<'py, PyString>, templates: &Bound<'py, PyString>, filename: &Bound<'py, PyString>, mode: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    document_part(tpl,true,partname,templates,filename,mode)
}

#[pyfunction]
pub(crate) fn composer_footnote_part<'py>(tpl: &Bound<'py, PyAny>, partname: &Bound<'py, PyString>, templates: &Bound<'py, PyString>, filename: &Bound<'py, PyString>, mode: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    document_part(tpl,false,partname,templates,filename,mode)
}

#[pyfunction]
pub(crate) fn composer_add_footnotes<'py>(tpl: &Bound<'py, PyAny>, doc: &Bound<'py, PyAny>, element: &Bound<'py, PyAny>, reference_query: &Bound<'py, PyString>, note_query: &Bound<'py, PyString>) -> PyResult<()> {
    let py=tpl.py();
    let deps=dependencies(py)?;
    let scope=composer_scope(py,deps)?;
    let find=element.getattr(pyo3::intern!(py,"findall"))?;
    let ns=global(&scope,pyo3::intern!(py,"NS"))?;
    let references=find.call1((reference_query,ns))?;
    if !references.is_truthy()? { return Ok(()); }
    let rels=doc.getattr(pyo3::intern!(py,"part"))?.getattr(pyo3::intern!(py,"rels"))?;
    let find=rels.getattr(pyo3::intern!(py,"part_with_reltype"))?;
    let kind=global(&scope,pyo3::intern!(py,"RT"))?.getattr(pyo3::intern!(py,"FOOTNOTES"))?;
    let source=find.call1((kind,))?;
    let target=tpl.call_method0(pyo3::intern!(py,"footnote_part"))?;
    let parse=global(&scope,pyo3::intern!(py,"parse_xml"))?;
    let footnotes=parse.call1((target.getattr(pyo3::intern!(py,"blob"))?,))?;
    let length=global(&scope,pyo3::intern!(py,"len"))?.call1((&footnotes,))?;
    let one=1_i32.into_pyobject(py)?;
    let mut next=unsafe { Bound::from_owned_ptr_or_err(py,pyo3::ffi::PyNumber_Add(length.as_ptr(),one.as_ptr()))? };
    let mut last_element=element.clone();
    for reference in references.try_iter()? {
        let reference=reference?;
        let get=reference.getattr(pyo3::intern!(py,"get"))?;
        let id=get.call1((word_name(&scope,"id")?,))?;
        let parse=global(&scope,pyo3::intern!(py,"parse_xml"))?;
        last_element=parse.call1((source.getattr(pyo3::intern!(py,"blob"))?,))?;
        let copy=global(&scope,pyo3::intern!(py,"deepcopy"))?;
        let find=last_element.getattr(pyo3::intern!(py,"find"))?;
        let query=note_query.call_method1(pyo3::intern!(py,"__mod__"),(id,))?;
        let ns=global(&scope,pyo3::intern!(py,"NS"))?;
        let note=copy.call1((find.call1((query,ns))?,))?;
        footnotes.call_method1(pyo3::intern!(py,"append"),(&note,))?;
        for node in [&note,&reference] {
            let set=node.getattr(pyo3::intern!(py,"set"))?;
            let name=word_name(&scope,"id")?;
            let string=global(&scope,pyo3::intern!(py,"str"))?;
            let id=string.call1((&next,))?;
            set.call1((name,id))?;
        }
        next=unsafe { Bound::from_owned_ptr_or_err(py,pyo3::ffi::PyNumber_InPlaceAdd(next.as_ptr(),one.as_ptr()))? };
    }
    tpl.call_method1(pyo3::intern!(py,"add_referenced_parts"),(&source,&target,last_element))?;
    let serialize=global(&scope,pyo3::intern!(py,"serialize_part_xml"))?;
    let blob=serialize.call1((footnotes,))?;
    target.setattr(pyo3::intern!(py,"_blob"),blob)?;
    Ok(())
}

#[pyfunction]
pub(crate) fn composer_next_numbering_ids<'py>(tpl: &Bound<'py, PyAny>, num_query: &Bound<'py, PyString>, abstract_query: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyTuple>> {
    let py=tpl.py();
    let deps=dependencies(py)?;
    let scope=composer_scope(py,deps)?;
    let part=tpl.call_method0(pyo3::intern!(py,"numbering_part"))?;
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    let root=part.getattr(pyo3::intern!(py,"element"))?;
    let nodes=composer_xpath(&xpath,&root,num_query,deps)?;
    let nums=PyList::empty(py);
    for node in nodes.try_iter()? { nums.append(node?.getattr(pyo3::intern!(py,"numId"))?)?; }
    let next=|values: &Bound<'py, PyList>, empty: i32| -> PyResult<Bound<'py, PyAny>> {
        if values.is_empty() { return Ok(empty.into_pyobject(py)?.into_any()); }
        let maximum=global(&scope,pyo3::intern!(py,"max"))?.call1((values,))?;
        let one=1_i32.into_pyobject(py)?;
        unsafe { Bound::from_owned_ptr_or_err(py,pyo3::ffi::PyNumber_Add(maximum.as_ptr(),one.as_ptr())) }
    };
    let num_id=next(&nums,1)?;
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    let root=part.getattr(pyo3::intern!(py,"element"))?;
    let nodes=composer_xpath(&xpath,&root,abstract_query,deps)?;
    let abstracts=PyList::empty(py);
    for node in nodes.try_iter()? {
        let node=node?;
        let integer=global(&scope,pyo3::intern!(py,"int"))?;
        abstracts.append(integer.call1((node,))?)?;
    }
    let abstract_id=next(&abstracts,0)?;
    PyTuple::new(py,[num_id,abstract_id])
}

fn word_name<'py>(scope: &Bound<'py, PyDict>, suffix: &str) -> PyResult<Bound<'py, PyAny>> {
    let py=scope.py();
    let ns=global(scope,pyo3::intern!(py,"NS"))?;
    let uri=ns.get_item(pyo3::intern!(py,"w"))?;
    PyString::new(py,&format!("{{%s}}{suffix}")).call_method1(pyo3::intern!(py,"__mod__"),(uri,))
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub(crate) fn composer_add_numberings<'py>(tpl: &Bound<'py, PyAny>, doc: &Bound<'py, PyAny>, element: &Bound<'py, PyAny>, ref_query: &Bound<'py, PyString>, num_query: &Bound<'py, PyString>, abstract_ref_query: &Bound<'py, PyString>, abstract_query: &Bound<'py, PyString>, nsid_query: &Bound<'py, PyString>) -> PyResult<()> {
    let py=tpl.py();
    let deps=dependencies(py)?;
    let scope=composer_scope(py,deps)?;
    let set=global(&scope,pyo3::intern!(py,"set"))?;
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    let refs=composer_xpath(&xpath,element,ref_query,deps)?;
    let ids=PyList::empty(py);
    for item in refs.try_iter()? { ids.append(item?.getattr(pyo3::intern!(py,"val"))?)?; }
    let ids=set.call1((ids,))?;
    if !ids.is_truthy()? { return Ok(()); }
    let next=tpl.call_method0(pyo3::intern!(py,"_next_numbering_ids"))?;
    let (next_num,next_abstract)=unpack_pair(&next)?;
    let source=doc.getattr(pyo3::intern!(py,"part"))?.getattr(pyo3::intern!(py,"numbering_part"))?;
    for id in ids.try_iter()? {
        let id=id?;
        if tpl.getattr(pyo3::intern!(py,"num_id_mapping"))?.contains(&id)? { continue; }
        let xpath=source.getattr(pyo3::intern!(py,"element"))?.getattr(pyo3::intern!(py,"xpath"))?;
        let query=num_query.call_method1(pyo3::intern!(py,"__mod__"),(&id,))?;
        let found=xpath.call1((query,))?;
        if !found.is_truthy()? { continue; }
        let copy=global(&scope,pyo3::intern!(py,"deepcopy"))?;
        let num=copy.call1((found.get_item(0)?,))?;
        num.setattr(pyo3::intern!(py,"numId"),&next_num)?;
        tpl.getattr(pyo3::intern!(py,"num_id_mapping"))?.set_item(&id,&next_num)?;
        let abstract_ref=num.call_method1(pyo3::intern!(py,"xpath"),(abstract_ref_query,))?.get_item(0)?;
        let abstract_value=abstract_ref.getattr(pyo3::intern!(py,"val"))?;
        if !tpl.getattr(pyo3::intern!(py,"anum_id_mapping"))?.contains(abstract_value)? {
            let xpath=source.getattr(pyo3::intern!(py,"element"))?.getattr(pyo3::intern!(py,"xpath"))?;
            let query=abstract_query.call_method1(pyo3::intern!(py,"__mod__"),(abstract_ref.getattr(pyo3::intern!(py,"val"))?,))?;
            let found=xpath.call1((query,))?;
            if !found.is_truthy()? { continue; }
            let copy=global(&scope,pyo3::intern!(py,"deepcopy"))?;
            let abstract_num=copy.call1((found.get_item(0)?,))?;
            let mapping=tpl.getattr(pyo3::intern!(py,"anum_id_mapping"))?;
            let key=abstract_ref.getattr(pyo3::intern!(py,"val"))?;
            mapping.set_item(key,&next_abstract)?;
            abstract_ref.setattr(pyo3::intern!(py,"val"),&next_abstract)?;
            let set=abstract_num.getattr(pyo3::intern!(py,"set"))?;
            let name=word_name(&scope,"abstractNumId")?;
            let string=global(&scope,pyo3::intern!(py,"str"))?;
            let value=string.call1((&next_abstract,))?;
            set.call1((name,value))?;
            let find=abstract_num.getattr(pyo3::intern!(py,"find"))?;
            let ns=global(&scope,pyo3::intern!(py,"NS"))?;
            let nsid=find.call1((nsid_query,ns))?;
            if !nsid.is_none() {
                let set=nsid.getattr(pyo3::intern!(py,"set"))?;
                let name=word_name(&scope,"val")?;
                // docxcompose draws a random nsid; derive it from the copied
                // definition instead so renders are reproducible.
                let definition: Vec<u8>=py.import(pyo3::intern!(py,"lxml.etree"))?
                    .getattr(pyo3::intern!(py,"tostring"))?.call1((&abstract_num,))?.extract()?;
                let master=tpl.call_method0(pyo3::intern!(py,"numbering_part"))?.getattr(pyo3::intern!(py,"element"))?;
                let taken: Vec<String>=master.call_method1(pyo3::intern!(py,"xpath"),(".//w:nsid/@w:val",))?.extract()?;
                let text=docxtplrs::numbering::stable_nsid(&definition,&|nsid| taken.iter().any(|t| t.eq_ignore_ascii_case(nsid)));
                set.call1((name,text))?;
            }
            tpl.call_method1(pyo3::intern!(py,"_insert_abstract_num"),(abstract_num,))?;
        } else {
            let mapping=tpl.getattr(pyo3::intern!(py,"anum_id_mapping"))?;
            let key=abstract_ref.getattr(pyo3::intern!(py,"val"))?;
            let value=mapping.get_item(key)?;
            abstract_ref.setattr(pyo3::intern!(py,"val"),value)?;
        }
        tpl.call_method1(pyo3::intern!(py,"_insert_num"),(num,))?;
    }
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    let refs=composer_xpath(&xpath,element,ref_query,deps)?;
    for reference in refs.try_iter()? {
        let reference=reference?;
        let get=tpl.getattr(pyo3::intern!(py,"num_id_mapping"))?.getattr(pyo3::intern!(py,"get"))?;
        let key=reference.getattr(pyo3::intern!(py,"val"))?;
        let default=reference.getattr(pyo3::intern!(py,"val"))?;
        let value=get.call1((key,default))?;
        reference.setattr(pyo3::intern!(py,"val"),value)?;
    }
    Ok(())
}

fn style_ids<'py>(tpl: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
    let py=tpl.py();
    let styles=tpl.getattr(pyo3::intern!(py,"doc"))?.getattr(pyo3::intern!(py,"styles"))?;
    let ids=PyList::empty(py);
    for style in styles.try_iter()? { ids.append(style?.getattr(pyo3::intern!(py,"style_id"))?)?; }
    Ok(ids)
}

fn lookup_style<'py>(doc: &Bound<'py, PyAny>, id: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py=doc.py();
    doc.getattr(pyo3::intern!(py,"styles"))?.getattr(pyo3::intern!(py,"element"))?
        .call_method1(pyo3::intern!(py,"get_by_id"),(id,))
}

fn append_style<'py>(tpl: &Bound<'py, PyAny>, style: &Bound<'py, PyAny>) -> PyResult<()> {
    let py=tpl.py();
    tpl.getattr(pyo3::intern!(py,"doc"))?.getattr(pyo3::intern!(py,"styles"))?.getattr(pyo3::intern!(py,"element"))?
        .call_method1(pyo3::intern!(py,"append"),(style,))?;
    Ok(())
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub(crate) fn composer_add_styles<'py>(tpl: &Bound<'py, PyAny>, doc: &Bound<'py, PyAny>, element: &Bound<'py, PyAny>, used_query: &Bound<'py, PyString>, linked_query: &Bound<'py, PyString>, num_query: &Bound<'py, PyString>, abstract_query: &Bound<'py, PyString>, replace_query: &Bound<'py, PyString>) -> PyResult<()> {
    let py=tpl.py();
    let deps=dependencies(py)?;
    let scope=composer_scope(py,deps)?;
    let mut our_ids=style_ids(tpl)?;
    let list=global(&scope,pyo3::intern!(py,"list"))?;
    let ordered=global(&scope,pyo3::intern!(py,"OrderedDict"))?;
    let fromkeys=ordered.getattr(pyo3::intern!(py,"fromkeys"))?;
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    let nodes=composer_xpath(&xpath,element,used_query,deps)?;
    let ids=PyList::empty(py);
    for node in nodes.try_iter()? { ids.append(node?.getattr(pyo3::intern!(py,"val"))?)?; }
    let used=list.call1((fromkeys.call1((ids,))?,))?;
    for style_id in used.try_iter()? {
        let style_id=style_id?;
        let our_id=tpl.call_method1(pyo3::intern!(py,"mapped_style_id"),(&style_id,))?;
        if !our_ids.contains(&our_id)? {
            let copy=global(&scope,pyo3::intern!(py,"deepcopy"))?;
            let style=copy.call1((lookup_style(doc,&style_id)?,))?;
            if !style.is_none() {
                append_style(tpl,&style)?;
                tpl.call_method1(pyo3::intern!(py,"add_numberings"),(doc,&style))?;
                let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
                let linked=composer_xpath(&xpath,&style,linked_query,deps)?;
                if linked.is_truthy()? {
                    let linked_id=linked.get_item(0)?;
                    let mapped=tpl.call_method1(pyo3::intern!(py,"mapped_style_id"),(&linked_id,))?;
                    if !our_ids.contains(&mapped)? {
                        let linked_style=lookup_style(doc,&linked_id)?;
                        if !linked_style.is_none() {
                            let append=tpl.getattr(pyo3::intern!(py,"doc"))?.getattr(pyo3::intern!(py,"styles"))?.getattr(pyo3::intern!(py,"element"))?.getattr(pyo3::intern!(py,"append"))?;
                            let copy=global(&scope,pyo3::intern!(py,"deepcopy"))?;
                            append.call1((copy.call1((linked_style,))?,))?;
                        }
                    }
                }
            }
        } else {
            let style=lookup_style(doc,&style_id)?;
            if !style.is_none() {
                let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
                let num_ids=composer_xpath(&xpath,&style,num_query,deps)?;
                if num_ids.is_truthy()? {
                    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
                    let root=doc.getattr(pyo3::intern!(py,"part"))?.getattr(pyo3::intern!(py,"numbering_part"))?.getattr(pyo3::intern!(py,"element"))?;
                    let query=abstract_query.call_method1(pyo3::intern!(py,"__mod__"),(num_ids.get_item(0)?,))?.cast_into::<PyString>()?;
                    let abstract_ids=composer_xpath(&xpath,&root,&query,deps)?;
                    if abstract_ids.is_truthy()? {
                        let master=tpl.getattr(pyo3::intern!(py,"doc"))?;
                        let our_style=lookup_style(&master,&our_id)?;
                        let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
                        let our_num_ids=composer_xpath(&xpath,&our_style,num_query,deps)?;
                        if our_num_ids.is_truthy()? {
                            let part=tpl.call_method0(pyo3::intern!(py,"numbering_part"))?;
                            let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
                            let root=part.getattr(pyo3::intern!(py,"element"))?;
                            let query=abstract_query.call_method1(pyo3::intern!(py,"__mod__"),(our_num_ids.get_item(0)?,))?.cast_into::<PyString>()?;
                            let our_abstract_ids=composer_xpath(&xpath,&root,&query,deps)?;
                            if our_abstract_ids.is_truthy()? {
                                let integer=global(&scope,pyo3::intern!(py,"int"))?;
                                let value=integer.call1((our_abstract_ids.get_item(0)?,))?;
                                let mapping=tpl.getattr(pyo3::intern!(py,"anum_id_mapping"))?;
                                let integer=global(&scope,pyo3::intern!(py,"int"))?;
                                let key=integer.call1((abstract_ids.get_item(0)?,))?;
                                mapping.set_item(key,value)?;
                            }
                        }
                    }
                }
            }
        }
        if our_id.ne(&style_id)? && !our_id.is_none() {
            let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
            let dictionary=global(&scope,pyo3::intern!(py,"dict"))?;
            let kwargs=PyDict::new(py);
            kwargs.set_item(pyo3::intern!(py,"styleid"),&style_id)?;
            let replacement=dictionary.call((),Some(&kwargs))?;
            let query=replace_query.call_method1(pyo3::intern!(py,"__mod__"),(replacement,))?.cast_into::<PyString>()?;
            let nodes=composer_xpath(&xpath,element,&query,deps)?;
            for node in nodes.try_iter()? { node?.setattr(pyo3::intern!(py,"val"),&our_id)?; }
        }
        our_ids=style_ids(tpl)?;
    }
    Ok(())
}

#[pyfunction]
pub(crate) fn composer_restart_first_numbering<'py>(tpl: &Bound<'py, PyAny>, _doc: &Bound<'py, PyAny>, element: &Bound<'py, PyAny>, queries: &Bound<'py, PyTuple>) -> PyResult<()> {
    let py=tpl.py();
    if !tpl.getattr(pyo3::intern!(py,"restart_numbering"))?.is_truthy()? { return Ok(()); }
    let deps=dependencies(py)?;
    let scope=composer_scope(py,deps)?;
    let query=|index| -> PyResult<Bound<'py, PyString>> { Ok(queries.get_item(index)?.cast_into::<PyString>()?) };
    let formatted=|index,value: &Bound<'py, PyAny>| -> PyResult<Bound<'py, PyString>> {
        Ok(query(index)?.call_method1(pyo3::intern!(py,"__mod__"),(value,))?.cast_into::<PyString>()?)
    };
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    let styles=composer_xpath(&xpath,element,&query(0)?,deps)?;
    if !styles.is_truthy()? { return Ok(()); }
    let style_id=styles.get_item(0)?;
    if tpl.getattr(pyo3::intern!(py,"_numbering_restarted"))?.contains(&style_id)? { return Ok(()); }
    let master=tpl.getattr(pyo3::intern!(py,"doc"))?;
    let style=lookup_style(&master,&style_id)?;
    if style.is_none() { return Ok(()); }
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    if composer_xpath(&xpath,&style,&query(1)?,deps)?.is_truthy()? { return Ok(()); }
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    let local=composer_xpath(&xpath,element,&query(2)?,deps)?;
    let num_id=if local.is_truthy()? { local.get_item(0)? } else {
        let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
        let style_nums=composer_xpath(&xpath,&style,&query(3)?,deps)?;
        if !style_nums.is_truthy()? { return Ok(()); }
        style_nums.get_item(0)?
    };
    let part=tpl.call_method0(pyo3::intern!(py,"numbering_part"))?;
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    let root=part.getattr(pyo3::intern!(py,"element"))?;
    let nums=composer_xpath(&xpath,&root,&formatted(4,&num_id)?,deps)?;
    if !nums.is_truthy()? { return Ok(()); }
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    let num=nums.get_item(0)?;
    let abstract_ids=composer_xpath(&xpath,&num,&query(5)?,deps)?;
    let abstract_id=abstract_ids.get_item(0)?;
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    let root=part.getattr(pyo3::intern!(py,"element"))?;
    let abstracts=composer_xpath(&xpath,&root,&formatted(6,&abstract_id)?,deps)?;
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    let abstract_num=abstracts.get_item(0)?;
    let formats=composer_xpath(&xpath,&abstract_num,&query(7)?,deps)?;
    if formats.is_truthy()? && formats.get_item(0)?.eq(pyo3::intern!(py,"bullet"))? { return Ok(()); }
    let copy=global(&scope,pyo3::intern!(py,"deepcopy"))?;
    let new_num=copy.call1((nums.get_item(0)?,))?;
    let parse=global(&scope,pyo3::intern!(py,"parse_xml"))?;
    let override_element=parse.call1((query(8)?,))?;
    new_num.call_method1(pyo3::intern!(py,"append"),(override_element,))?;
    let next=tpl.call_method0(pyo3::intern!(py,"_next_numbering_ids"))?;
    let (next_num,_next_abstract)=unpack_pair(&next)?;
    new_num.setattr(pyo3::intern!(py,"numId"),&next_num)?;
    tpl.call_method1(pyo3::intern!(py,"_insert_num"),(new_num,))?;
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    let paragraphs=composer_xpath(&xpath,element,&formatted(9,&style_id)?,deps)?;
    let xpath=global(&scope,pyo3::intern!(py,"xpath"))?;
    let paragraph=paragraphs.get_item(0)?;
    let nums=composer_xpath(&xpath,&paragraph,&query(10)?,deps)?;
    if nums.is_truthy()? {
        let num=nums.get_item(0)?;
        let previous=num.getattr(pyo3::intern!(py,"numId"))?.getattr(pyo3::intern!(py,"val"))?;
        tpl.call_method1(pyo3::intern!(py,"_replace_mapped_num_id"),(previous,&next_num))?;
        num.getattr(pyo3::intern!(py,"numId"))?.setattr(pyo3::intern!(py,"val"),&next_num)?;
    } else {
        let parse=global(&scope,pyo3::intern!(py,"parse_xml"))?;
        let new_props=parse.call1((formatted(11,&next_num)?,))?;
        paragraphs.get_item(0)?.call_method1(pyo3::intern!(py,"append"),(new_props,))?;
    }
    tpl.getattr(pyo3::intern!(py,"_numbering_restarted"))?.call_method1(pyo3::intern!(py,"add"),(style_id,))?;
    Ok(())
}

fn part_name_groups(value: &Bound<'_, PyAny>) -> PyResult<(String,Option<String>)> {
    let text=value.extract::<String>()?;
    let bytes=text.as_bytes();
    let prefix=bytes.iter().take_while(|&&b| b.is_ascii_alphabetic() || matches!(b,b'/'|b'_'|b'-')).count();
    if prefix==0 { return Err(pyo3::exceptions::PyAttributeError::new_err("'NoneType' object has no attribute 'group'")); }
    let mut end=prefix;
    if bytes.get(end).is_some_and(|b| matches!(b,b'1'..=b'9')) {
        end+=1;
        while bytes.get(end).is_some_and(u8::is_ascii_digit) { end+=1; }
    }
    Ok((text[..prefix].to_owned(),(end>prefix).then(|| text[prefix..end].to_owned())))
}

fn relationship_number(value: &Bound<'_, PyAny>) -> PyResult<String> {
    let text=value.extract::<String>()?;
    let Some(suffix)=text.strip_prefix("rId") else {
        return Err(pyo3::exceptions::PyAttributeError::new_err("'NoneType' object has no attribute 'group'"));
    };
    let end=suffix.as_bytes().iter().take_while(|b| b.is_ascii_digit()).count();
    if end==0 { return Err(pyo3::exceptions::PyValueError::new_err("invalid literal for int() with base 10: ''")); }
    let normalized=suffix[..end].trim_start_matches('0');
    Ok(if normalized.is_empty() { "0".to_owned() } else { normalized.to_owned() })
}

#[pyfunction]
pub(crate) fn composer_add_relationship(tpl: &Bound<'_, PyAny>, dst: &Bound<'_, PyAny>, relationship: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let py=dst.py();
    if relationship.getattr(pyo3::intern!(py,"is_external"))?.is_truthy()? {
        let rels=dst.getattr(pyo3::intern!(py,"rels"))?;
        let add=rels.getattr(pyo3::intern!(py,"get_or_add_ext_rel"))?;
        let kind=relationship.getattr(pyo3::intern!(py,"reltype"))?;
        let target=relationship.getattr(pyo3::intern!(py,"target_ref"))?;
        let id=add.call1((kind,target))?;
        return Ok(dst.getattr(pyo3::intern!(py,"rels"))?.get_item(id)?.unbind());
    }
    let deps=dependencies(py)?;
    let scope=composer_scope(py,deps)?;
    let part=relationship.getattr(pyo3::intern!(py,"target_part"))?;
    let (name,_)=part_name_groups(&part.getattr(pyo3::intern!(py,"partname"))?)?;
    let parts=dst.getattr(pyo3::intern!(py,"package"))?.call_method0(pyo3::intern!(py,"iter_parts"))?;
    let mut used=HashSet::new();
    let mut used_count=0usize;
    for existing in parts.try_iter()? {
        let existing=existing?;
        let partname=existing.getattr(pyo3::intern!(py,"partname"))?;
        if partname.call_method1(pyo3::intern!(py,"startswith"),(&name,))?.is_truthy()? {
            if let Some(number)=part_name_groups(&existing.getattr(pyo3::intern!(py,"partname"))?)?.1 {
                used_count+=1;
                used.insert(number);
            }
        }
    }
    let number=(1..=used_count+1).find(|n| !used.contains(&n.to_string())).expect("a free part number exists");
    let packuri=global(&scope,pyo3::intern!(py,"PackURI"))?;
    let extension=part.getattr(pyo3::intern!(py,"partname"))?.getattr(pyo3::intern!(py,"ext"))?;
    let path=PyString::new(py,"%s%d.%s").call_method1(pyo3::intern!(py,"__mod__"),((name,number,extension),))?;
    let partname=packuri.call1((path,))?;
    let part_class=global(&scope,pyo3::intern!(py,"Part"))?;
    let content_type=part.getattr(pyo3::intern!(py,"content_type"))?;
    let blob=part.getattr(pyo3::intern!(py,"blob"))?;
    let package=dst.getattr(pyo3::intern!(py,"package"))?;
    let new_part=part_class.call1((partname,content_type,blob,package))?;
    let add=dst.getattr(pyo3::intern!(py,"rels"))?.getattr(pyo3::intern!(py,"get_or_add"))?;
    let kind=relationship.getattr(pyo3::intern!(py,"reltype"))?;
    let new_rel=add.call1((kind,&new_part))?;
    let rels=part.getattr(pyo3::intern!(py,"rels"))?.call_method0(pyo3::intern!(py,"values"))?;
    let mut nested=Vec::new();
    for rel in rels.try_iter()? {
        let rel=rel?;
        let number=relationship_number(&rel.getattr(pyo3::intern!(py,"rId"))?)?;
        nested.push((number,rel));
    }
    nested.sort_by(|a,b| a.0.len().cmp(&b.0.len()).then_with(|| a.0.cmp(&b.0)));
    for (_,rel) in nested { tpl.call_method1(pyo3::intern!(py,"add_relationship"),(&part,&new_part,rel))?; }
    Ok(new_rel.unbind())
}

#[pyfunction]
pub(crate) fn composer_create_style_id_mapping(tpl: &Bound<'_, PyAny>, doc: &Bound<'_, PyAny>) -> PyResult<()> {
    let py=tpl.py();
    let id_to_name=PyDict::new(py);
    for style in doc.getattr(pyo3::intern!(py,"styles"))?.try_iter()? {
        let style=style?;
        let id=style.getattr(pyo3::intern!(py,"style_id"))?;
        let name=style.getattr(pyo3::intern!(py,"name"))?;
        id_to_name.set_item(id,name)?;
    }
    tpl.setattr(pyo3::intern!(py,"_style_id2name"),id_to_name)?;
    let name_to_id=PyDict::new(py);
    let styles=tpl.getattr(pyo3::intern!(py,"doc"))?.getattr(pyo3::intern!(py,"styles"))?;
    for style in styles.try_iter()? {
        let style=style?;
        let name=style.getattr(pyo3::intern!(py,"name"))?;
        let id=style.getattr(pyo3::intern!(py,"style_id"))?;
        name_to_id.set_item(name,id)?;
    }
    tpl.setattr(pyo3::intern!(py,"_style_name2id"),name_to_id)?;
    Ok(())
}

#[pyfunction]
pub(crate) fn composer_mapped_style_id(tpl: &Bound<'_, PyAny>, style_id: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let py=tpl.py();
    if !tpl.getattr(pyo3::intern!(py,"_style_id2name"))?.contains(style_id)? { return Ok(style_id.clone().unbind()); }
    let mapping=tpl.getattr(pyo3::intern!(py,"_style_name2id"))?;
    if mapping.is_exact_instance_of::<PyDict>() {
        let name=tpl.getattr(pyo3::intern!(py,"_style_id2name"))?.get_item(style_id)?;
        return Ok(mapping.cast::<PyDict>()?.get_item(name)?.unwrap_or_else(|| style_id.clone()).unbind());
    }
    let get=mapping.getattr(pyo3::intern!(py,"get"))?;
    let name=tpl.getattr(pyo3::intern!(py,"_style_id2name"))?.get_item(style_id)?;
    Ok(get.call1((name,style_id))?.unbind())
}

#[pyfunction]
pub(crate) fn composer_add_diagrams(tpl: &Bound<'_, PyAny>, doc: &Bound<'_, PyAny>, element: &Bound<'_, PyAny>, scope: &Bound<'_, PyDict>, query: &Bound<'_, PyString>) -> PyResult<()> {
    let py=tpl.py();
    let deps=dependencies(py)?;
    let diagrams=native_xpath(element,query,deps)?;
    for diagram in diagrams.try_iter()? {
        let diagram=diagram?;
        let kinds=[("dm",global(scope,pyo3::intern!(py,"RT"))?.getattr(pyo3::intern!(py,"DIAGRAM_DATA"))?),
            ("lo",global(scope,pyo3::intern!(py,"RT"))?.getattr(pyo3::intern!(py,"DIAGRAM_LAYOUT"))?),
            ("qs",global(scope,pyo3::intern!(py,"RT"))?.getattr(pyo3::intern!(py,"DIAGRAM_QUICK_STYLE"))?),
            ("cs",global(scope,pyo3::intern!(py,"RT"))?.getattr(pyo3::intern!(py,"DIAGRAM_COLORS"))?)];
        for (key,kind) in kinds {
            let get=diagram.getattr(pyo3::intern!(py,"get"))?;
            let ns=global(scope,pyo3::intern!(py,"NS"))?;
            let name=format!("{{{}}}{}",ns.get_item(pyo3::intern!(py,"r"))?.str()?.to_str()?,key);
            let rid=get.call1((&name,))?;
            let target=doc.getattr(pyo3::intern!(py,"part"))?.getattr(pyo3::intern!(py,"rels"))?
                .get_item(&rid)?.getattr(pyo3::intern!(py,"target_part"))?;
            let new_id=tpl.getattr(pyo3::intern!(py,"doc"))?.getattr(pyo3::intern!(py,"part"))?
                .call_method1(pyo3::intern!(py,"relate_to"),(target,kind))?;
            let set=diagram.getattr(pyo3::intern!(py,"set"))?;
            let ns=global(scope,pyo3::intern!(py,"NS"))?;
            let name=format!("{{{}}}{}",ns.get_item(pyo3::intern!(py,"r"))?.str()?.to_str()?,key);
            set.call1((name,new_id))?;
        }
    }
    Ok(())
}
