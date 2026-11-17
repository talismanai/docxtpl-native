//! Borrow a contiguous Python buffer while its exporter and the GIL are held.
use pyo3::prelude::*;

pub(crate) fn with_simple_buffer<T>(data: &Bound<'_, PyAny>, operation: impl FnOnce(&[u8]) -> T) -> PyResult<T> {
    let py=data.py();
    let mut buffer=pyo3::ffi::Py_buffer::new();
    if unsafe { pyo3::ffi::PyObject_GetBuffer(data.as_ptr(),&mut buffer,pyo3::ffi::PyBUF_SIMPLE) } != 0 {
        return Err(PyErr::fetch(py));
    }
    struct Guard(pyo3::ffi::Py_buffer);
    impl Drop for Guard { fn drop(&mut self) { unsafe { pyo3::ffi::PyBuffer_Release(&mut self.0) } } }
    let guard=Guard(buffer);
    let bytes=if guard.0.len==0 { &[] } else {
        unsafe { std::slice::from_raw_parts(guard.0.buf.cast::<u8>(),guard.0.len as usize) }
    };
    Ok(operation(bytes))
}

#[pyfunction]
pub(crate) fn base64_encode(data: &Bound<'_, PyAny>) -> PyResult<String> {
    use base64::Engine;
    with_simple_buffer(data, |bytes| base64::engine::general_purpose::STANDARD.encode(bytes))
}
