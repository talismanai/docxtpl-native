//! Per-part XML transformation for the exposed Python document model.
use pyo3::prelude::*;
use pyo3::types::PyString;

fn unicode_replace<'py>(source: Bound<'py, PyString>, old: &str, new: &str) -> PyResult<Bound<'py, PyString>> {
    let py = source.py();
    let old = PyString::new(py, old);
    let new = PyString::new(py, new);
    // SAFETY: all three pointers are live Unicode objects and the GIL is held.
    let result = unsafe { Bound::from_owned_ptr_or_err(py, pyo3::ffi::PyUnicode_Replace(
        source.as_ptr(), old.as_ptr(), new.as_ptr(), -1))? };
    Ok(result.cast_into::<PyString>()?)
}

fn add_paragraphs<'py>(source: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyString>> {
    let py = source.py();
    match source.to_str() {
        Ok(text) if !text.contains("<w:p ") && !text.contains("<w:p>") => {
            if source.is_exact_instance_of::<PyString>() { Ok(source.clone()) }
            else { Ok(PyString::new(py, text)) }
        }
        Ok(text) => Ok(PyString::new(py, &docxtplrs::template::add_paragraph_newlines(text))),
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyUnicodeEncodeError>(py) => {
            let source = unicode_replace(source.clone(), "<w:p ", "\n<w:p ")?;
            unicode_replace(source, "<w:p>", "\n<w:p>")
        }
        Err(error) => Err(error),
    }
}

fn finish_part<'py>(source: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyString>> {
    let py = source.py();
    let text = match source.to_str() {
        Ok(text) => {
            let without_breaks = docxtplrs::template::remove_paragraph_newlines(text);
            docxtplrs::template::restore_escaped_delims(without_breaks.into_owned())
        }
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyUnicodeEncodeError>(py) => {
            let length = unsafe { pyo3::ffi::PyUnicode_GetLength(source.as_ptr()) };
            if length < 0 { return Err(PyErr::fetch(py)); }
            let allocation = unsafe { pyo3::ffi::PyUnicode_AsUCS4Copy(source.as_ptr()) };
            if allocation.is_null() { return Err(PyErr::fetch(py)); }
            struct UnicodeAllocation(*mut pyo3::ffi::Py_UCS4);
            impl Drop for UnicodeAllocation {
                fn drop(&mut self) { unsafe { pyo3::ffi::PyMem_Free(self.0.cast()) } }
            }
            let allocation = UnicodeAllocation(allocation);
            let input = unsafe { std::slice::from_raw_parts(allocation.0, length as usize) };
            let pieces = docxtpl_core::unicode_transport::transform_fragments(&[
                docxtpl_core::unicode_transport::Fragment::Text(input),
            ]).map_err(crate::error)?;
            let mut output = Vec::new();
            for piece in pieces {
                match piece {
                    docxtpl_core::unicode_transport::OwnedFragment::Text(text) => output.extend(text),
                    docxtpl_core::unicode_transport::OwnedFragment::Deferred(_) => {
                        return Err(pyo3::exceptions::PyRuntimeError::new_err("unexpected deferred value in live XML"));
                    }
                }
            }
            let length = isize::try_from(output.len()).map_err(crate::error)?;
            let value = unsafe { Bound::from_owned_ptr_or_err(py, pyo3::ffi::PyUnicode_FromKindAndData(
                4, output.as_ptr().cast(), length))? };
            return Ok(value.cast_into::<PyString>()?);
        }
        Err(error) => return Err(error),
    };
    Ok(PyString::new(py, &crate::python_session::resolve_listing_xml(&text)))
}

#[pyfunction]
pub(crate) fn render_live_xml<'py>(document: &Bound<'py, PyAny>, session: &Bound<'py, PyAny>,
    source: &Bound<'py, PyAny>, part: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyString>> {
    let py = document.py();
    document.setattr(pyo3::intern!(py, "current_rendering_part"), part)?;
    let patch = document.getattr(pyo3::intern!(py, "patch_xml"))?;
    let patched = patch.call1((source,))?;
    let patched = crate::regex_string(&patched)?;
    let source = add_paragraphs(patched)?;
    let render = session.getattr(pyo3::intern!(py, "render"))?;
    let scope = crate::pyglobals::FrameGlobals::current(py)?;
    let stringify = crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "str"))?;
    let name = stringify.call1((part.getattr(pyo3::intern!(py, "partname"))?,))?
        .call_method1(pyo3::intern!(py, "lstrip"), ("/",))?;
    let rendered = render.call1((name, source))?;
    finish_part(crate::regex_string(&rendered)?)
}
