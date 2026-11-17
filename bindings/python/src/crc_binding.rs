//! CPython file and buffer protocols around the native CRC implementation.
use pyo3::prelude::*;
use pyo3::types::{PyString, PyType};

/// The `with` statement looks up special methods on the type, then binds the
/// descriptor to the instance; it does not use the instance's attribute hook.
fn lookup_special<'py>(object: &Bound<'py, PyAny>, name: &Bound<'py, PyString>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = object.py();
    let cls = object.get_type();
    let type_getattr = py.get_type::<PyType>().getattr(pyo3::intern!(py, "__getattribute__"))?;
    let mro = type_getattr.call1((&cls, pyo3::intern!(py, "__mro__")))?;
    for base in mro.try_iter()? {
        let dict = type_getattr.call1((base?, pyo3::intern!(py, "__dict__")))?;
        if !dict.contains(name)? { continue; }
        let descriptor = dict.get_item(name)?;
        // PyType_GetSlot returns the descriptor's C-level binding function.
        // The descriptor, instance and type are live while the GIL is held.
        let slot = unsafe { pyo3::ffi::PyType_GetSlot(pyo3::ffi::Py_TYPE(descriptor.as_ptr()), pyo3::ffi::Py_tp_descr_get) };
        if slot.is_null() {
            if unsafe { !pyo3::ffi::PyErr_Occurred().is_null() } { return Err(PyErr::fetch(py)); }
            return Ok(Some(descriptor));
        }
        type DescriptorGet = unsafe extern "C" fn(*mut pyo3::ffi::PyObject, *mut pyo3::ffi::PyObject, *mut pyo3::ffi::PyObject) -> *mut pyo3::ffi::PyObject;
        let get: DescriptorGet = unsafe { std::mem::transmute(slot) };
        let bound = unsafe { get(descriptor.as_ptr(), object.as_ptr(), cls.as_ptr()) };
        return Ok(Some(unsafe { Bound::from_owned_ptr_or_err(py, bound) }?));
    }
    Ok(None)
}

fn read_with_context<'py>(source: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = source.py();
    let open = py.import("builtins")?.getattr(pyo3::intern!(py, "open"))?;
    let manager = open.call1((source, pyo3::intern!(py, "rb")))?;
    match read_context_manager(&manager)? {
        Some(data) => Ok(data),
        None => Err(pyo3::exceptions::PyUnboundLocalError::new_err(
            "cannot access local variable 'buf' where it is not associated with a value")),
    }
}

pub(crate) fn read_context_manager<'py>(manager: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    with_context_manager(manager, |file| file.call_method0(pyo3::intern!(file.py(), "read")))
}

pub(crate) fn with_context_manager<'py, T>(manager: &Bound<'py, PyAny>, body: impl FnOnce(&Bound<'py, PyAny>) -> PyResult<T>) -> PyResult<Option<T>> {
    let py=manager.py();
    let enter = match lookup_special(manager, pyo3::intern!(py, "__enter__"))? {
        Some(method) => method,
        None => return Err(pyo3::exceptions::PyTypeError::new_err(format!("'{}' object does not support the context manager protocol", manager.get_type().name()?))),
    };
    let exit = match lookup_special(manager, pyo3::intern!(py, "__exit__"))? {
        Some(method) => method,
        None => return Err(pyo3::exceptions::PyTypeError::new_err(format!("'{}' object does not support the context manager protocol (missed __exit__ method)", manager.get_type().name()?))),
    };
    let file = enter.call0()?;
    match body(&file) {
        Ok(data) => { exit.call1((py.None(), py.None(), py.None()))?; Ok(Some(data)) }
        Err(error) => {
            let traceback = error.traceback(py).map(|tb| tb.into_any().unbind()).unwrap_or_else(|| py.None());
            let guard = super::pyglobals::HandledException::new(py,&error);
            let handled = exit.call1((error.get_type(py), error.value(py), traceback))
                .and_then(|result| result.is_truthy());
            drop(guard);
            match handled {
                Ok(true) => Ok(None),
                Ok(false) => Err(error),
                Err(exit_error) => {
                    // The with statement chains an __exit__ failure to the active exception.
                    Err(super::pyglobals::chain_context(py,&error,exit_error))
                }
            }
        }
    }
}

#[pyfunction]
pub(crate) fn crc_file_source(source: &Bound<'_, PyAny>) -> PyResult<u32> {
    let py = source.py();
    let data = if source.hasattr(pyo3::intern!(py, "read"))? {
        source.call_method0(pyo3::intern!(py, "read"))?
    } else { read_with_context(source)? };
    super::buffer_binding::with_simple_buffer(&data,docxtplrs::package::crc32)
}
