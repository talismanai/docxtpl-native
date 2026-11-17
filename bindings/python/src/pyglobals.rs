//! Resolve Python function globals and their builtins fallback.
use pyo3::prelude::*;
use pyo3::types::{PyDict,PyModule,PyString,PyTuple};

/// The exception being handled (`sys.exc_info()`) for the guard's lifetime.
///
/// `PyErr_SetHandledException` takes its own reference (it does not steal
/// one) and `PyErr_GetHandledException` returns a new one, released on drop.
/// An extra reference here would keep every handled exception, with its
/// traceback, frames and their locals, alive forever.
pub(crate) struct HandledException(*mut pyo3::ffi::PyObject);

impl HandledException {
    pub(crate) fn new(py: Python<'_>, error: &PyErr) -> Self {
        let previous=unsafe { pyo3::ffi::PyErr_GetHandledException() };
        unsafe { pyo3::ffi::PyErr_SetHandledException(error.value(py).as_ptr()) };
        Self(previous)
    }
}

impl Drop for HandledException {
    fn drop(&mut self) {
        unsafe {
            pyo3::ffi::PyErr_SetHandledException(self.0);
            pyo3::ffi::Py_XDECREF(self.0);
        }
    }
}

pub(crate) fn chain_context(py: Python<'_>, original: &PyErr, error: PyErr) -> PyErr {
    if error.value(py).as_ptr()!=original.value(py).as_ptr() {
        unsafe {
            pyo3::ffi::Py_INCREF(original.value(py).as_ptr());
            pyo3::ffi::PyException_SetContext(error.value(py).as_ptr(),original.value(py).as_ptr());
        }
    }
    error
}

pub(crate) fn chain_context_if_missing(py: Python<'_>, original: &PyErr, error: PyErr) -> PyErr {
    let context=unsafe { pyo3::ffi::PyException_GetContext(error.value(py).as_ptr()) };
    if context.is_null() { chain_context(py,original,error) } else {
        unsafe { pyo3::ffi::Py_DECREF(context) };
        error
    }
}

pub(crate) struct FrameGlobals<'py> {
    globals: Bound<'py, PyDict>,
    builtins: Bound<'py, PyDict>,
}

impl<'py> FrameGlobals<'py> {
    pub(crate) fn of_function(function: &Bound<'py, PyAny>) -> PyResult<Self> {
        let py = function.py();
        Ok(Self {
            globals: function.getattr(pyo3::intern!(py, "__globals__"))?.cast_into::<PyDict>()?,
            // A function retains its builtins dictionary even when the globals
            // dictionary's __builtins__ entry is subsequently rebound.
            builtins: function.getattr(pyo3::intern!(py, "__builtins__"))?.cast_into::<PyDict>()?,
        })
    }
    pub(crate) fn current(py: Python<'py>) -> PyResult<Self> {
        let globals=unsafe { pyo3::ffi::PyEval_GetGlobals() };
        let builtins=unsafe { pyo3::ffi::PyEval_GetBuiltins() };
        if globals.is_null() || builtins.is_null() {
            return Err(pyo3::exceptions::PyRuntimeError::new_err("binding globals are unavailable"));
        }
        Ok(Self {
            globals: unsafe { Bound::from_borrowed_ptr(py,globals) }.cast_into::<PyDict>()?,
            builtins: unsafe { Bound::from_borrowed_ptr(py,builtins) }.cast_into::<PyDict>()?,
        })
    }
    pub(crate) fn py(&self) -> Python<'py> { self.globals.py() }
}

pub(crate) fn frame_global<'py>(scope: &FrameGlobals<'py>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    if let Some(value)=scope.globals.get_item(name)? { return Ok(value); }
    if let Some(value)=scope.builtins.get_item(name)? { return Ok(value); }
    Err(missing_name_error(name))
}

pub(crate) fn global<'py>(scope: &Bound<'py, PyDict>, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    if let Some(value)=scope.get_item(name)? { return Ok(value); }
    if let Some(builtins)=scope.get_item(pyo3::intern!(scope.py(),"__builtins__"))? {
        let found=if let Ok(dict)=builtins.cast::<PyDict>() { dict.get_item(name)? }
            else if let Ok(module)=builtins.cast::<PyModule>() { module.dict().get_item(name)? }
            else { None };
        if let Some(value)=found { return Ok(value); }
    }
    Err(missing_name_error(name))
}

fn missing_name_error(name: &Bound<'_, PyString>) -> PyErr {
    let py = name.py();
    let result = (|| -> PyResult<PyErr> {
        let keywords = PyDict::new(py);
        keywords.set_item("name", name)?;
        let error = py.get_type::<pyo3::exceptions::PyNameError>()
            .call((format!("name '{}' is not defined", name),), Some(&keywords))?;
        Ok(PyErr::from_value(error))
    })();
    match result { Ok(error) | Err(error) => error }
}

fn catches(scope: &FrameGlobals<'_>, error: &PyErr, names: &[&str]) -> PyResult<bool> {
    let py = scope.py();
    // Resolve every operand before validating the except target, as the Python
    // bytecode does. A single operand may itself be a tuple of exception types.
    let values = names
        .iter()
        .map(|name| frame_global(scope, &pyo3::types::PyString::new(py, name)))
        .collect::<PyResult<Vec<_>>>()?;
    let target = if values.len() == 1 {
        values[0].clone()
    } else {
        PyTuple::new(py, values)?.into_any()
    };
    let valid = if let Ok(tuple) = target.cast::<PyTuple>() {
        tuple
            .iter()
            .all(|value| unsafe { pyo3::ffi::PyExceptionClass_Check(value.as_ptr()) != 0 })
    } else {
        unsafe { pyo3::ffi::PyExceptionClass_Check(target.as_ptr()) != 0 }
    };
    if !valid {
        return Err(pyo3::exceptions::PyTypeError::new_err(
            "catching classes that do not inherit from BaseException is not allowed",
        ));
    }
    Ok(error.is_instance(py, &target))
}

pub(crate) fn handle_exception<'py, T>(
    scope: &FrameGlobals<'py>,
    error: PyErr,
    names: &[&str],
    handler: impl FnOnce() -> PyResult<T>,
) -> PyResult<T> {
    let py = scope.py();
    let _handled = HandledException::new(py, &error);
    let result = (|| {
        if catches(scope, &error, names)? {
            handler()
        } else {
            Err(error.clone_ref(py))
        }
    })();
    result.map_err(|failure| chain_context_if_missing(py, &error, failure))
}
