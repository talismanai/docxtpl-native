//! Exposed document traversal for undeclared-variable analysis.
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use super::pyglobals::{frame_global, FrameGlobals};

fn unpack_pair<'py>(value: &Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let mut iter=match value.try_iter() {
        Ok(iter) => iter,
        Err(error) => {
            let ty=value.get_type();
            let iter_slot=unsafe { pyo3::ffi::PyType_GetSlot(ty.as_type_ptr(),pyo3::ffi::Py_tp_iter) };
            let item_slot=unsafe { pyo3::ffi::PyType_GetSlot(ty.as_type_ptr(),pyo3::ffi::Py_sq_item) };
            if error.is_instance_of::<pyo3::exceptions::PyTypeError>(value.py()) && iter_slot.is_null() && item_slot.is_null() {
                return Err(pyo3::exceptions::PyTypeError::new_err(format!("cannot unpack non-iterable {} object",ty.name()?)));
            }
            return Err(error);
        }
    };
    let first=iter.next().transpose()?.ok_or_else(|| pyo3::exceptions::PyValueError::new_err("not enough values to unpack (expected 2, got 0)"))?;
    let second=iter.next().transpose()?.ok_or_else(|| pyo3::exceptions::PyValueError::new_err("not enough values to unpack (expected 2, got 1)"))?;
    if iter.next().transpose()?.is_some() { return Err(pyo3::exceptions::PyValueError::new_err("too many values to unpack (expected 2)")); }
    Ok((first,second))
}

fn inplace_add<'py>(left: &Bound<'py, PyAny>, right: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    unsafe { Bound::from_owned_ptr_or_err(left.py(), pyo3::ffi::PyNumber_InPlaceAdd(left.as_ptr(),right.as_ptr())) }
}

#[pyfunction]
pub(crate) fn undeclared_variables<'py>(tpl: &Bound<'py, PyAny>, jinja_env: &Bound<'py, PyAny>, context: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py=tpl.py();
    let scope=FrameGlobals::current(py)?;
    let document=frame_global(&scope,pyo3::intern!(py,"Document"))?;
    let template=tpl.getattr(pyo3::intern!(py,"template_file"))?;
    let document=document.call1((template,))?;
    let serialize=tpl.getattr(pyo3::intern!(py,"xml_to_string"))?;
    let body=document.getattr(pyo3::intern!(py,"_element"))?.getattr(pyo3::intern!(py,"body"))?;
    let xml=serialize.call1((body,))?;
    let mut xml=tpl.call_method1(pyo3::intern!(py,"patch_xml"),(xml,))?;
    let header=tpl.getattr(pyo3::intern!(py,"HEADER_URI"))?;
    let footer=tpl.getattr(pyo3::intern!(py,"FOOTER_URI"))?;
    let uris=PyTuple::new(py,[header,footer])?;
    let mut _last_pair=None;
    for uri in uris.iter() {
        let rels=document.getattr(pyo3::intern!(py,"_part"))?.getattr(pyo3::intern!(py,"rels"))?;
        let items=rels.call_method0(pyo3::intern!(py,"items"))?;
        for item in items.try_iter()? {
            let pair=unpack_pair(&item?)?;
            _last_pair=Some(pair);
            let rel=&_last_pair.as_ref().unwrap().1;
            if !rel.getattr(pyo3::intern!(py,"reltype"))?.eq(&uri)? { continue; }
            let blob=rel.getattr(pyo3::intern!(py,"target_part"))?.getattr(pyo3::intern!(py,"blob"))?;
            if !blob.is_truthy()? { continue; }
            let serialize=tpl.getattr(pyo3::intern!(py,"xml_to_string"))?;
            let parse=frame_global(&scope,pyo3::intern!(py,"parse_xml"))?;
            let blob=rel.getattr(pyo3::intern!(py,"target_part"))?.getattr(pyo3::intern!(py,"blob"))?;
            let parsed=parse.call1((blob,))?;
            let part_xml=serialize.call1((parsed,))?;
            let patched=tpl.call_method1(pyo3::intern!(py,"patch_xml"),(part_xml,))?;
            xml=inplace_add(&xml,&patched)?;
        }
    }
    let environment=if jinja_env.is_truthy()? { jinja_env.clone() } else {
        frame_global(&scope,pyo3::intern!(py,"Environment"))?.call0()?
    };
    let tree=environment.call_method1(pyo3::intern!(py,"parse"),(xml,))?;
    let meta=frame_global(&scope,pyo3::intern!(py,"meta"))?;
    let values=meta.call_method1(pyo3::intern!(py,"find_undeclared_variables"),(tree,))?;
    if context.is_none() { return Ok(values); }
    let set=frame_global(&scope,pyo3::intern!(py,"set"))?;
    let keys=context.call_method0(pyo3::intern!(py,"keys"))?;
    let provided=set.call1((keys,))?;
    unsafe { Bound::from_owned_ptr_or_err(py,pyo3::ffi::PyNumber_Subtract(values.as_ptr(),provided.as_ptr())) }
}
