//! Keyword unpacking uses CPython's dictionary protocol at the binding boundary.
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};

fn type_error(message: &Bound<'_, PyAny>) -> PyErr {
    // The interpreter also chains the currently handled exception here.
    // Constructing a TypeError instance alone would omit that context.
    unsafe { pyo3::ffi::PyErr_SetObject(pyo3::ffi::PyExc_TypeError, message.as_ptr()); }
    PyErr::fetch(message.py())
}

/// CPython snapshots and validates all keyword names before parameter matching.
pub(crate) fn snapshot<'py>(kwargs: &Bound<'py, PyDict>) -> PyResult<Bound<'py, PyList>> {
    let pairs = kwargs.items();
    for pair in pairs.iter() {
        if !pair.get_item(0)?.is_instance_of::<PyString>() {
            return Err(type_error(PyString::new(kwargs.py(), "keywords must be strings").as_any()));
        }
    }
    Ok(pairs)
}

pub(crate) fn parameter_index(key: &Bound<'_, PyAny>, parameters: &[Bound<'_, PyAny>]) -> PyResult<Option<usize>> {
    if let Some(index) = parameters.iter().position(|parameter| key.is(parameter)) { return Ok(Some(index)); }
    for (index, parameter) in parameters.iter().enumerate() {
        if key.eq(parameter)? { return Ok(Some(index)); }
    }
    Ok(None)
}

pub(crate) fn argument_error(function: &Bound<'_, PyAny>, key: &Bound<'_, PyAny>, message: &str) -> PyResult<PyErr> {
    let py = function.py();
    // Snapshot the function name before %S invokes the keyword's __str__.
    let name = function.getattr(pyo3::intern!(py, "__qualname__"))?;
    let keyword = key.str()?;
    let message = crate::python_operators::join_strings(py, vec![
        name, PyString::new(py, message).into_any(), keyword.into_any(),
        PyString::new(py, "'").into_any(),
    ], None)?;
    Ok(type_error(&message))
}

pub(crate) fn function_error(function: &Bound<'_, PyAny>, message: &str) -> PyResult<PyErr> {
    let py = function.py();
    let message = crate::python_operators::join_strings(py, vec![
        function.getattr(pyo3::intern!(py, "__qualname__"))?, PyString::new(py, message).into_any(),
    ], None)?;
    Ok(type_error(&message))
}

// The supported wheels target CPython 3.11/3.12, not abi3. Reuse the same
// exported dictionary merge and callable formatting primitives as CALL_EX.
unsafe extern "C" {
    fn _PyDict_MergeEx(target: *mut pyo3::ffi::PyObject, source: *mut pyo3::ffi::PyObject, overwrite: std::ffi::c_int) -> std::ffi::c_int;
    fn _PyObject_FunctionStr(value: *mut pyo3::ffi::PyObject) -> *mut pyo3::ffi::PyObject;
}

pub(crate) fn merge<'py>(
    callable: &Bound<'py, PyAny>, sources: &Bound<'py, PyList>,
) -> PyResult<Bound<'py, PyDict>> {
    let py = callable.py();
    let result = PyDict::new(py);
    for source in sources.iter() {
        // SAFETY: GIL held; both owned Bound references outlive the C call.
        if unsafe { _PyDict_MergeEx(result.as_ptr(), source.as_ptr(), 2) } == 0 { continue; }
        return Err(merge_error(callable, &source)?);
    }
    Ok(result)
}

#[allow(deprecated)] // PyErr_Fetch/Restore retain the unnormalized 3.11 error.
fn merge_error(callable: &Bound<'_, PyAny>, source: &Bound<'_, PyAny>) -> PyResult<PyErr> {
    let py = callable.py();
    let mut raw_error = None;
    let mut normalized_error = None;
    // Match each interpreter's format_kwargs_error (PSF license in
    // vendor/notices). 3.11 recognizes an unnormalized one-item tuple;
    // 3.12 examines a normalized exception's args slot instead.
    let key = if unsafe { pyo3::ffi::PyErr_ExceptionMatches(pyo3::ffi::PyExc_AttributeError) } != 0 {
        unsafe { pyo3::ffi::PyErr_Clear(); }
        None
    } else if unsafe { pyo3::ffi::PyErr_ExceptionMatches(pyo3::ffi::PyExc_KeyError) } == 0 {
        return Ok(PyErr::fetch(py));
    } else if py.version_info().minor < 12 {
        let (mut exception, mut value, mut traceback) =
            (std::ptr::null_mut(), std::ptr::null_mut(), std::ptr::null_mut());
        unsafe { pyo3::ffi::PyErr_Fetch(&mut exception, &mut value, &mut traceback); }
        let one_tuple = !value.is_null() && unsafe {
            pyo3::ffi::PyTuple_Check(value) != 0 && pyo3::ffi::PyTuple_Size(value) == 1
        };
        if !one_tuple {
            unsafe { pyo3::ffi::PyErr_Restore(exception, value, traceback); }
            return Ok(PyErr::fetch(py));
        }
        let value = unsafe { Bound::from_owned_ptr(py, value) };
        let key = value.cast::<PyTuple>()?.get_item(0)?;
        raw_error = Some((
            unsafe { Bound::from_owned_ptr_or_opt(py, exception) }, value,
            unsafe { Bound::from_owned_ptr_or_opt(py, traceback) },
        ));
        Some(key)
    } else {
        let error = PyErr::fetch(py);
        // Read BaseException's slot without a subclass attribute hook.
        let pointer = unsafe { (*(error.value(py).as_ptr().cast::<pyo3::ffi::PyBaseExceptionObject>())).args };
        let key = if pointer.is_null() { None } else {
            let args = unsafe { Bound::from_borrowed_ptr(py, pointer) };
            match args.cast::<PyTuple>() {
                Ok(args) if args.len() == 1 => Some(args.get_item(0)?),
                _ => None,
            }
        };
        if key.is_none() { return Ok(error); }
        normalized_error = Some(error);
        key
    };
    // Retain the original KeyError/tuple through callable and key formatting.
    let _original = (raw_error, normalized_error);
    let name = unsafe { Bound::from_owned_ptr_or_err(py, _PyObject_FunctionStr(callable.as_ptr()))? };
    let message = if let Some(key) = key {
        unsafe { Bound::from_owned_ptr_or_err(py, pyo3::ffi::PyUnicode_FromFormat(
            c"%U got multiple values for keyword argument '%S'".as_ptr(), name.as_ptr(), key.as_ptr()))? }
    } else {
        unsafe { Bound::from_owned_ptr_or_err(py, pyo3::ffi::PyUnicode_FromFormat(
            c"%U argument after ** must be a mapping, not %.200s".as_ptr(), name.as_ptr(),
            (*pyo3::ffi::Py_TYPE(source.as_ptr())).tp_name))? }
    };
    Ok(type_error(&message))
}
