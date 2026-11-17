//! Convert Python context values into the native payload representation.
use pyo3::prelude::*;
use pyo3::types::{PyCFunction,PyDict,PyList,PyString,PyTuple};
use super::pyglobals::{FrameGlobals as ValueScope,frame_global as global};

fn unsupported<'py>(scope: &ValueScope<'py>, message: &Bound<'py, PyAny>) -> PyErr {
    let py=scope.py();
    match global(scope,pyo3::intern!(py,"UnsupportedFeature")).and_then(|class| class.call1((message,))) {
        Ok(value) => PyErr::from_value(value),
        Err(error) => error,
    }
}

fn type_is<'py>(scope: &ValueScope<'py>, value: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<bool> {
    let py=scope.py();
    let type_fn=global(scope,pyo3::intern!(py,"type"))?;
    let kind=type_fn.call1((value,))?;
    let expected=global(scope,name)?;
    Ok(kind.is(&expected))
}

fn instance_of(function: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>, classes: &Bound<'_, PyAny>) -> PyResult<bool> {
    function.call1((value,classes))?.is_truthy()
}

fn payload<'py>(py: Python<'py>, kind: &str) -> PyResult<Bound<'py, PyDict>> {
    let output=PyDict::new(py);
    let name=match kind {
        "xml" => pyo3::intern!(py,"xml"), "image" => pyo3::intern!(py,"image"),
        "subdoc" => pyo3::intern!(py,"subdoc"), "none" => pyo3::intern!(py,"none"),
        "bool" => pyo3::intern!(py,"bool"), "int" => pyo3::intern!(py,"int"),
        "float" => pyo3::intern!(py,"float"), "str" => pyo3::intern!(py,"str"),
        "list" => pyo3::intern!(py,"list"), "map" => pyo3::intern!(py,"map"),
        _ => unreachable!("payload kinds are fixed"),
    };
    output.set_item(pyo3::intern!(py,"kind"),name)?;
    Ok(output)
}

fn encode_source<'py>(scope: &ValueScope<'py>, value: &Bound<'py, PyAny>, attribute: &str) -> PyResult<Bound<'py, PyAny>> {
    let py=scope.py();
    let encode=global(scope,pyo3::intern!(py,"_b64"))?;
    let read=global(scope,pyo3::intern!(py,"_read"))?;
    let source=value.getattr(attribute)?;
    let data=read.call1((source,))?;
    encode.call1((data,))
}

fn next_depth<'py>(depth: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py=depth.py();
    let one=1i32.into_pyobject(py)?;
    unsafe { Bound::from_owned_ptr_or_err(py,pyo3::ffi::PyNumber_Add(depth.as_ptr(),one.as_ptr())) }
}

fn recurse<'py>(scope: &ValueScope<'py>, item: &Bound<'py, PyAny>, depth: &Bound<'py, PyAny>, active: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py=scope.py();
    let function=global(scope,pyo3::intern!(py,"_value"))?;
    let depth=next_depth(depth)?;
    let kwargs=PyDict::new(py);
    kwargs.set_item(pyo3::intern!(py,"depth"),depth)?;
    kwargs.set_item(pyo3::intern!(py,"active"),active)?;
    function.call((item,),Some(&kwargs))
}

fn remove_active<'py>(scope: &ValueScope<'py>, active: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>) -> PyResult<()> {
    let py=scope.py();
    let remove=active.getattr(pyo3::intern!(py,"remove"))?;
    let id=global(scope,pyo3::intern!(py,"id"))?.call1((value,))?;
    remove.call1((id,))?;
    Ok(())
}

fn container<'py>(scope: &ValueScope<'py>, value: &Bound<'py, PyAny>, depth: &Bound<'py, PyAny>, active: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py=scope.py();
    let id=global(scope,pyo3::intern!(py,"id"))?.call1((value,))?;
    if active.contains(&id)? { return Err(unsupported(scope,PyString::new(py,"cyclic context").as_any())); }
    let add=active.getattr(pyo3::intern!(py,"add"))?;
    let id=global(scope,pyo3::intern!(py,"id"))?.call1((value,))?;
    add.call1((id,))?;
    let result=(|| -> PyResult<Bound<'py, PyAny>> {
        if type_is(scope,value,pyo3::intern!(py,"list"))? {
            let output=payload(py,"list")?;
            let items=PyList::empty(py);
            for item in value.try_iter()? { items.append(recurse(scope,&item?,depth,active)?)?; }
            output.set_item(pyo3::intern!(py,"items"),items)?;
            return Ok(output.into_any());
        }
        for key in value.try_iter()? {
            if !type_is(scope,&key?,pyo3::intern!(py,"str"))? { return Err(unsupported(scope,PyString::new(py,"non-string mapping key").as_any())); }
        }
        let output=payload(py,"map")?;
        let items=PyList::empty(py);
        let pairs=value.call_method0(pyo3::intern!(py,"items"))?;
        for pair in pairs.try_iter()? {
            let pair=pair?;
            let key=pair.get_item(0)?;
            let item=pair.get_item(1)?;
            let converted=recurse(scope,&item,depth,active)?;
            items.append(PyList::new(py,[key,converted])?)?;
        }
        output.set_item(pyo3::intern!(py,"items"),items)?;
        Ok(output.into_any())
    })();
    let cleanup=remove_active(scope,active,value);
    match (result,cleanup) {
        (Ok(value),Ok(())) => Ok(value),
        (Err(error),Ok(())) => Err(error),
        (Ok(_),Err(error)) => Err(error),
        (Err(original),Err(error)) => {
            if error.value(py).as_ptr()!=original.value(py).as_ptr() {
                unsafe {
                    pyo3::ffi::Py_INCREF(original.value(py).as_ptr());
                    pyo3::ffi::PyException_SetContext(error.value(py).as_ptr(),original.value(py).as_ptr());
                }
            }
            Err(error)
        }
    }
}

#[pyfunction]
pub(crate) fn encode_value<'py>(value: &Bound<'py, PyAny>, depth: &Bound<'py, PyAny>, active: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py=value.py();
    // The caller is the thin `_value` binding; its frame retains the module
    // globals even if the Python name `globals` has been rebound.
    let scope=ValueScope::current(py)?;
    if depth.gt(100)? { return Err(unsupported(&scope,PyString::new(py,"context depth above 100").as_any())); }
    let active=if active.is_none() { global(&scope,pyo3::intern!(py,"set"))?.call0()? } else { active.clone() };
    let isinstance=global(&scope,pyo3::intern!(py,"isinstance"))?;
    let rich=global(&scope,pyo3::intern!(py,"RichText"))?;
    let paragraph=global(&scope,pyo3::intern!(py,"RichTextParagraph"))?;
    let listing=global(&scope,pyo3::intern!(py,"Listing"))?;
    let classes=PyTuple::new(py,[rich,paragraph,listing])?;
    if instance_of(&isinstance,value,classes.as_any())? {
        let output=payload(py,"xml")?;
        output.set_item(pyo3::intern!(py,"value"),global(&scope,pyo3::intern!(py,"str"))?.call1((value,))?)?;
        return Ok(output.into_any());
    }
    let isinstance=global(&scope,pyo3::intern!(py,"isinstance"))?;
    let image=global(&scope,pyo3::intern!(py,"InlineImage"))?;
    if instance_of(&isinstance,value,&image)? {
        let output=payload(py,"image")?;
        output.set_item(pyo3::intern!(py,"data"),encode_source(&scope,value,"image_descriptor")?)?;
        let isinstance=global(&scope,pyo3::intern!(py,"isinstance"))?;
        let descriptor=value.getattr(pyo3::intern!(py,"image_descriptor"))?;
        let string=global(&scope,pyo3::intern!(py,"str"))?;
        let pathlike=global(&scope,pyo3::intern!(py,"os"))?.getattr(pyo3::intern!(py,"PathLike"))?;
        let classes=PyTuple::new(py,[string,pathlike])?;
        let filename=if isinstance.call1((descriptor,classes))?.is_truthy()? {
            let path=global(&scope,pyo3::intern!(py,"Path"))?;
            let descriptor=value.getattr(pyo3::intern!(py,"image_descriptor"))?;
            path.call1((descriptor,))?.getattr(pyo3::intern!(py,"name"))?
        } else { py.None().into_bound(py) };
        output.set_item(pyo3::intern!(py,"filename"),filename)?;
        for attribute in ["width","height"] {
            let current=value.getattr(attribute)?;
            let converted=if current.is_none() { py.None().into_bound(py) } else {
                let int=global(&scope,pyo3::intern!(py,"int"))?;
                int.call1((value.getattr(attribute)?,))?
            };
            output.set_item(attribute,converted)?;
        }
        for attribute in ["anchor","title","descr"] {
            output.set_item(attribute,value.getattr(attribute)?)?;
        }
        return Ok(output.into_any());
    }
    let isinstance=global(&scope,pyo3::intern!(py,"isinstance"))?;
    let subdoc=global(&scope,pyo3::intern!(py,"Subdoc"))?;
    if instance_of(&isinstance,value,&subdoc)? {
        let output=payload(py,"subdoc")?;
        output.set_item(pyo3::intern!(py,"data"),encode_source(&scope,value,"source")?)?;
        return Ok(output.into_any());
    }
    if value.is_none() { return Ok(payload(py,"none")?.into_any()); }
    if type_is(&scope,value,pyo3::intern!(py,"bool"))? {
        let output=payload(py,"bool")?;output.set_item(pyo3::intern!(py,"value"),value)?;return Ok(output.into_any());
    }
    if type_is(&scope,value,pyo3::intern!(py,"int"))? {
        if value.extract::<i128>().is_err() { return Err(unsupported(&scope,PyString::new(py,"integer outside i128 range").as_any())); }
        let output=payload(py,"int")?;
        output.set_item(pyo3::intern!(py,"value"),global(&scope,pyo3::intern!(py,"str"))?.call1((value,))?)?;
        return Ok(output.into_any());
    }
    if type_is(&scope,value,pyo3::intern!(py,"float"))? {
        let check=global(&scope,pyo3::intern!(py,"math"))?.getattr(pyo3::intern!(py,"isfinite"))?;
        let builtin=check.is_exact_instance_of::<PyCFunction>()
            && check.getattr(pyo3::intern!(py,"__module__"))?.eq("math")?
            && check.getattr(pyo3::intern!(py,"__name__"))?.eq("isfinite")?;
        let finite=if builtin { value.extract::<f64>()?.is_finite() } else { check.call1((value,))?.is_truthy()? };
        if !finite { return Err(unsupported(&scope,PyString::new(py,"non-finite float").as_any())); }
        let output=payload(py,"float")?;output.set_item(pyo3::intern!(py,"value"),value)?;return Ok(output.into_any());
    }
    if type_is(&scope,value,pyo3::intern!(py,"str"))? {
        let output=payload(py,"str")?;output.set_item(pyo3::intern!(py,"value"),value)?;return Ok(output.into_any());
    }
    let type_fn=global(&scope,pyo3::intern!(py,"type"))?;
    let kind=type_fn.call1((value,))?;
    let list=global(&scope,pyo3::intern!(py,"list"))?;
    let dict=global(&scope,pyo3::intern!(py,"dict"))?;
    if PyTuple::new(py,[list,dict])?.contains(kind)? { return container(&scope,value,depth,&active); }
    let type_fn=global(&scope,pyo3::intern!(py,"type"))?;
    let module=type_fn.call1((value,))?.getattr(pyo3::intern!(py,"__module__"))?;
    let type_fn=global(&scope,pyo3::intern!(py,"type"))?;
    let name=type_fn.call1((value,))?.getattr(pyo3::intern!(py,"__name__"))?;
    let message=PyString::new(py,"context type {}.{}").call_method1(pyo3::intern!(py,"format"),(module,name))?;
    Err(unsupported(&scope,&message))
}
