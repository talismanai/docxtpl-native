//! Rich-value construction using Rust control flow and CPython object protocols.
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyInt, PyList, PyString, PyTuple};
use pyo3::exceptions::PyUnicodeEncodeError;

fn percent<'py>(py: Python<'py>, pattern: &str, values: &[Bound<'py, PyAny>]) -> PyResult<Bound<'py, PyAny>> {
    let format = PyString::new(py, pattern);
    let arguments = if values.len() == 1 { values[0].clone() } else { PyTuple::new(py, values)?.into_any() };
    unsafe { Bound::from_owned_ptr_or_err(py, pyo3::ffi::PyUnicode_Format(format.as_ptr(), arguments.as_ptr())) }
}

fn append<'py>(left: &Bound<'py, PyAny>, right: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    unsafe { Bound::from_owned_ptr_or_err(left.py(), pyo3::ffi::PyNumber_InPlaceAdd(left.as_ptr(), right.as_ptr())) }
}

fn append_xml(this: &Bound<'_, PyAny>, xml: &Bound<'_, PyAny>) -> PyResult<()> {
    this.setattr("xml", append(&this.getattr("xml")?, xml)?)
}

#[pyfunction]
pub(crate) fn init_rich_value(this: &Bound<'_, PyAny>, text: &Bound<'_, PyAny>, properties: &Bound<'_, PyDict>) -> PyResult<()> {
    this.setattr("xml", "")?;
    if text.is_truthy()? { this.call_method("add", (text,), Some(properties))?; }
    Ok(())
}

fn fast_run<'py>(py: Python<'py>, text: &Bound<'py, PyAny>, props: &[Bound<'py, PyAny>]) -> PyResult<Option<Bound<'py, PyAny>>> {
    if !text.is_exact_instance_of::<PyString>() { return Ok(None); }
    for index in [0,1,2,10,11,13] {
        if !props[index].is_none() && !props[index].is_exact_instance_of::<PyString>() { return Ok(None); }
    }
    for index in [4,5,6,7,9,12] {
        if !props[index].is_none() && !props[index].is_exact_instance_of::<PyBool>() { return Ok(None); }
    }
    if !props[3].is_none() && !props[3].is_exact_instance_of::<PyInt>() { return Ok(None); }
    let size = if props[3].is_none() { None } else { match props[3].extract::<u32>() { Ok(value) => Some(value).filter(|value| *value!=0), Err(_) => return Ok(None) } };
    if !props[8].is_none() && !props[8].is_exact_instance_of::<PyBool>() && !props[8].is_exact_instance_of::<PyString>() { return Ok(None); }
    let string = |index: usize| -> PyResult<Option<String>> {
        if props[index].is_none() { Ok(None) } else { Ok(Some(props[index].extract::<String>()?).filter(|value| !value.is_empty())) }
    };
    let build = || -> PyResult<String> {
        let underline = if props[8].is_exact_instance_of::<PyBool>() {
            props[8].is_truthy()?.then(|| "single".to_owned())
        } else { string(8)? };
        let properties = docxtplrs::richtext::TextProps {
            style: string(0)?, color: string(1)?, highlight: string(2)?, size,
            subscript: props[4].is_truthy()?, superscript: props[5].is_truthy()?,
            bold: props[6].is_truthy()?, italic: props[7].is_truthy()?, underline,
            strike: props[9].is_truthy()?, font: string(10)?, url_id: string(11)?,
            rtl: props[12].is_truthy()?, lang: string(13)?,
        };
        Ok(docxtplrs::richtext::richtext_run(text.extract::<&str>()?, &properties))
    };
    match build() {
        Ok(text) => Ok(Some(PyString::new(py,&text).into_any())),
        Err(error) if error.is_instance_of::<PyUnicodeEncodeError>(py) => Ok(None),
        Err(error) => Err(error),
    }
}

#[pyfunction]
pub(crate) fn add_richtext<'py>(py: Python<'py>, this: &Bound<'py, PyAny>, text: &Bound<'py, PyAny>, properties: &Bound<'py, PyTuple>, richtext_class: &Bound<'py, PyAny>) -> PyResult<()> {
    if text.is_instance(richtext_class)? {
        // Augmented assignment reads the destination before the right-hand
        // property; either getter may run user code or change the other value.
        let current = this.getattr("xml")?;
        let addition = text.getattr("xml")?;
        return this.setattr("xml", append(&current, &addition)?);
    }
    let props = properties.iter().collect::<Vec<_>>();
    if props.len()!=14 { return Err(pyo3::exceptions::PyTypeError::new_err("expected fourteen rich-text properties")); }
    if let Some(xml)=fast_run(py,text,&props)? { return append_xml(this,&xml); }
    let text = if text.is_instance(&py.get_type::<PyString>())? || text.is_instance(&py.get_type::<PyBytes>())? { text.clone() } else { text.str()?.into_any() };
    let text = if text.is_instance(&py.get_type::<PyString>())? { text } else {
        let kwargs=PyDict::new(py);kwargs.set_item("errors","ignore")?;
        text.call_method("decode",("utf-8",),Some(&kwargs))?
    };
    let escaped=crate::escape_html_text(py,&text)?;
    let mut prop=PyString::new(py,"").into_any();
    if props[0].is_truthy()? { prop=append(&prop,&percent(py,"<w:rStyle w:val=\"%s\"/>",&[props[0].clone()])?)?; }
    if props[10].is_truthy()? {
        let mut font=props[10].clone();let mut regional=PyString::new(py,"").into_any();
        if font.contains(":")? {
            let (region,split_font)=crate::unpack_two(&font.call_method1("split",(":",1))?)?;
            font=split_font.into_bound(py);
            let kwargs=PyDict::new(py);kwargs.set_item("font",&font)?;kwargs.set_item("region",region)?;
            regional=PyString::new(py," w:{region}=\"{font}\"").call_method("format",(),Some(&kwargs))?;
        }
        let kwargs=PyDict::new(py);kwargs.set_item("font",font)?;kwargs.set_item("regional_font",regional)?;
        let xml=PyString::new(py,"<w:rFonts w:ascii=\"{font}\" w:hAnsi=\"{font}\" w:cs=\"{font}\"{regional_font}/>").call_method("format",(),Some(&kwargs))?;
        prop=append(&prop,&xml)?;
    }
    for (index,pattern) in [(1,"<w:color w:val=\"%s\"/>"),(2,"<w:shd w:fill=\"%s\"/>")] {
        if props[index].is_truthy()? {
            let mut value=props[index].clone();
            if value.get_item(0)?.eq("#")? {
                let slice=py.get_type::<pyo3::types::PySlice>().call1((1,py.None(),py.None()))?;
                value=value.get_item(slice)?;
            }
            prop=append(&prop,&percent(py,pattern,&[value])?)?;
        }
    }
    if props[3].is_truthy()? {
        prop=append(&prop,&percent(py,"<w:sz w:val=\"%s\"/>",&[props[3].clone()])?)?;
        prop=append(&prop,&percent(py,"<w:szCs w:val=\"%s\"/>",&[props[3].clone()])?)?;
    }
    for (index,tag) in [(4,"<w:vertAlign w:val=\"subscript\"/>"),(5,"<w:vertAlign w:val=\"superscript\"/>")] {
        if props[index].is_truthy()? { prop=append(&prop,&PyString::new(py,tag).into_any())?; }
    }
    for (index,tag,rtl_tag) in [(6,"<w:b/>","<w:bCs/>"),(7,"<w:i/>","<w:iCs/>")] {
        if props[index].is_truthy()? {
            prop=append(&prop,&PyString::new(py,tag).into_any())?;
            if props[12].is_truthy()? { prop=append(&prop,&PyString::new(py,rtl_tag).into_any())?; }
        }
    }
    if props[8].is_truthy()? {
        let allowed=PyList::new(py,["single","double","thick","dotted","dash","dotDash","dotDotDash","wave"])?;
        let underline=if allowed.contains(&props[8])? { props[8].clone() } else { PyString::new(py,"single").into_any() };
        prop=append(&prop,&percent(py,"<w:u w:val=\"%s\"/>",&[underline])?)?;
    }
    if props[9].is_truthy()? { prop=append(&prop,&PyString::new(py,"<w:strike/>").into_any())?; }
    if props[12].is_truthy()? { prop=append(&prop,&PyString::new(py,"<w:rtl w:val=\"true\"/>").into_any())?; }
    if props[13].is_truthy()? { prop=append(&prop,&percent(py,"<w:lang w:val=\"%s\"/>",&[props[13].clone()])?)?; }
    let mut xml=PyString::new(py,"<w:r>").into_any();
    if prop.is_truthy()? { xml=append(&xml,&percent(py,"<w:rPr>%s</w:rPr>",&[prop])?)?; }
    xml=append(&xml,&percent(py,"<w:t xml:space=\"preserve\">%s</w:t></w:r>",&[escaped.into_bound(py)])?)?;
    if props[11].is_truthy()? { xml=percent(py,"<w:hyperlink r:id=\"%s\" w:tgtFrame=\"_blank\">%s</w:hyperlink>",&[props[11].clone(),xml])?; }
    append_xml(this,&xml)
}

#[pyfunction]
pub(crate) fn add_richtext_paragraph<'py>(py: Python<'py>, this: &Bound<'py, PyAny>, text: &Bound<'py, PyAny>, parastyle: &Bound<'py, PyAny>, richtext_class: &Bound<'py, PyAny>, native_richtext: &Bound<'py, PyAny>) -> PyResult<()> {
    let text=if text.is_instance(richtext_class)? { text.clone() } else { native_richtext.call1((text,))? };
    let mut xml=PyString::new(py,"<w:p>").into_any();
    if parastyle.is_truthy()? {
        let prop=percent(py,"<w:pStyle w:val=\"%s\"/>",&[parastyle.clone()])?;
        xml=append(&xml,&percent(py,"<w:pPr>%s</w:pPr>",&[prop])?)?;
    }
    xml=append(&xml,&text.getattr("xml")?)?;
    xml=append(&xml,&PyString::new(py,"</w:p>").into_any())?;
    append_xml(this,&xml)
}
