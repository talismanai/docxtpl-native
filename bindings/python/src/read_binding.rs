//! Source loading around Python path and stream protocols.
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyTuple};
use super::pyglobals::{FrameGlobals,frame_global};

unsafe extern "C" {
    fn PySys_Audit(event: *const std::ffi::c_char, format: *const std::ffi::c_char, ...) -> std::ffi::c_int;
}

// Recognize the standard implementation while preserving user replacements.
fn pathlib_method(method: &Bound<'_, PyAny>, name: &str) -> PyResult<bool> {
    if method.get_type().name()? != "method" { return Ok(false); }
    let function = method.getattr("__func__")?;
    if !function.getattr("__name__")?.eq(name)? { return Ok(false); }
    let globals = function.getattr("__globals__")?.cast_into::<PyDict>()?;
    Ok(globals.get_item("__name__")?.is_some_and(|module| module.eq("pathlib").unwrap_or(false)))
}

fn read_path<'py>(path: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = path.py();
    let read = path.getattr("read_bytes")?;
    if !pathlib_method(&read, "read_bytes")? { return read.call0(); }
    let open = path.getattr("open")?;
    if !pathlib_method(&open, "open")? {
        let kwargs = PyDict::new(py);
        kwargs.set_item("mode", "rb")?;
        return Ok(super::crc_binding::read_context_manager(&open.call((), Some(&kwargs))?)?.unwrap_or_else(|| py.None().into_bound(py)));
    }
    let globals = open.getattr("__func__")?.getattr("__globals__")?.cast_into::<PyDict>()?;
    let io = globals.get_item("io")?.ok_or_else(|| pyo3::exceptions::PyNameError::new_err("name 'io' is not defined"))?;
    let opener = io.getattr("open")?;
    let standard = opener.get_type().name()? == "builtin_function_or_method"
        && opener.getattr("__module__")?.eq("_io")?
        && opener.getattr("__name__")?.eq("open")?;
    if !standard {
        let manager = opener.call1((path, "rb", -1, py.None(), py.None(), py.None()))?;
        return Ok(super::crc_binding::read_context_manager(&manager)?.unwrap_or_else(|| py.None().into_bound(py)));
    }
    read_native_path(path)
}

fn read_native_path<'py>(path: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    use pyo3::{exceptions::{PyOSError, PyValueError}, ffi};
    let py = path.py();
    let filename = unsafe { Bound::from_owned_ptr_or_err(py, ffi::PyOS_FSPath(path.as_ptr()))? };
    #[cfg(unix)]
    let os_path = {
        use std::os::unix::ffi::OsStrExt;
        let bytes = if filename.is_instance_of::<PyBytes>() { filename.clone() } else {
            unsafe { Bound::from_owned_ptr_or_err(py, ffi::PyUnicode_EncodeFSDefault(filename.as_ptr()))? }
        };
        let bytes = bytes.cast::<PyBytes>()?.as_bytes();
        if bytes.contains(&0) { return Err(PyValueError::new_err("embedded null byte")); }
        std::path::PathBuf::from(std::ffi::OsStr::from_bytes(bytes))
    };
    #[cfg(windows)]
    let os_path = {
        use std::os::windows::ffi::OsStringExt;
        let mut length = 0;
        let wide = unsafe { ffi::PyUnicode_AsWideCharString(filename.as_ptr(), &mut length) };
        if wide.is_null() { return Err(PyErr::fetch(py)); }
        let units = unsafe { std::slice::from_raw_parts(wide, length as usize) }.to_vec();
        unsafe { ffi::PyMem_Free(wide.cast()) };
        if units.contains(&0) { return Err(PyValueError::new_err("embedded null character")); }
        std::path::PathBuf::from(std::ffi::OsString::from_wide(&units))
    };
    #[cfg(unix)]
    let flags = libc::O_CLOEXEC;
    #[cfg(windows)]
    let flags = 0;
    if unsafe { PySys_Audit(c"open".as_ptr(), c"Osi".as_ptr(), filename.as_ptr(), c"r".as_ptr(), flags) } < 0 {
        return Err(PyErr::fetch(py));
    }
    match py.detach(|| std::fs::read(os_path)) {
        Ok(data) => Ok(PyBytes::new(py, &data).into_any()),
        Err(error) => match error.raw_os_error() {
            Some(errno) => {
                let message = error.to_string();
                let suffix = format!(" (os error {errno})");
                let message = message.strip_suffix(&suffix).unwrap_or(&message);
                Err(PyErr::from_value(py.get_type::<PyOSError>().call1((errno, message, filename))?))
            }
            None => Err(PyOSError::new_err(error.to_string())),
        },
    }
}

#[pyfunction]
pub(crate) fn read_source(source: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let py=source.py();
    let scope=FrameGlobals::current(py)?;
    let isinstance=frame_global(&scope,pyo3::intern!(py,"isinstance"))?;
    let string=frame_global(&scope,pyo3::intern!(py,"str"))?;
    let pathlike=frame_global(&scope,pyo3::intern!(py,"os"))?.getattr(pyo3::intern!(py,"PathLike"))?;
    let classes=PyTuple::new(py,[string,pathlike])?;
    if isinstance.call1((source,classes))?.is_truthy()? {
        let path=frame_global(&scope,pyo3::intern!(py,"Path"))?.call1((source,))?;
        return Ok(read_path(&path)?.unbind());
    }
    let isinstance=frame_global(&scope,pyo3::intern!(py,"isinstance"))?;
    let bytes=frame_global(&scope,pyo3::intern!(py,"bytes"))?;
    if isinstance.call1((source,bytes))?.is_truthy()? { return Ok(source.clone().unbind()); }
    let hasattr=frame_global(&scope,pyo3::intern!(py,"hasattr"))?;
    if hasattr.call1((source,pyo3::intern!(py,"read")))?.is_truthy()? {
        let hasattr=frame_global(&scope,pyo3::intern!(py,"hasattr"))?;
        if hasattr.call1((source,pyo3::intern!(py,"seek")))?.is_truthy()? {
            let position=source.call_method0(pyo3::intern!(py,"tell"))?;
            let result=(|| -> PyResult<Bound<'_, PyAny>> {
                source.call_method1(pyo3::intern!(py,"seek"),(0,))?;
                source.call_method0(pyo3::intern!(py,"read"))
            })();
            let cleanup=match &result {
                Ok(_) => source.call_method1(pyo3::intern!(py,"seek"),(position,)),
                Err(error) => {
                    let guard=super::pyglobals::HandledException::new(py,error);
                    let result=source.call_method1(pyo3::intern!(py,"seek"),(position,));
                    drop(guard);
                    result
                }
            };
            return match (result,cleanup) {
                (Ok(value),Ok(_)) => Ok(value.unbind()),
                (Err(error),Ok(_)) => Err(error),
                (Ok(_),Err(error)) => Err(error),
                (Err(original),Err(error)) => {
                    Err(super::pyglobals::chain_context(py,&original,error))
                }
            };
        }
    }
    // As python-docx's zipfile.ZipFile(source): the archive seeks to the
    // end, tells the size and reads, so the first missing method raises its
    // own AttributeError.
    for method in [pyo3::intern!(py,"seek"),pyo3::intern!(py,"tell"),pyo3::intern!(py,"read")] {
        source.getattr(method)?;
    }
    let error=frame_global(&scope,pyo3::intern!(py,"TypeError"))?
        .call1(("expected path, bytes, or seekable binary file",))?;
    Err(PyErr::from_value(error))
}
