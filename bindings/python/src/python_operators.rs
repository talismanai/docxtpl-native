//! CPython object protocols for the native template runtime.
//!
//! Use the same interpreter operations as Python bytecode, preserving reflected
//! methods, rich-comparison results, arbitrary integers and original errors.
use pyo3::exceptions::PyKeyError;
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyList, PySlice, PyString, PyTuple};

pub(crate) fn join_strings<'py>(py: Python<'py>, values: Vec<Bound<'py, PyAny>>, separator: Option<&Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyAny>> {
    let sequence = PyList::new(py, values)?;
    let separator = separator.cloned().unwrap_or_else(|| PyString::new(py, "").into_any());
    // Unicode storage and type checking belong to the CPython object binding;
    // this preserves lone surrogates and the original join exception.
    unsafe { Bound::from_owned_ptr_or_err(py, ffi::PyUnicode_Join(separator.as_ptr(), sequence.as_ptr())) }
}

pub(crate) fn native_default_image_string<'py>(value: &Bound<'py, PyAny>, html: bool)
    -> PyResult<Option<Bound<'py, PyAny>>> {
    if value.is_exact_instance_of::<PyString>() || value.is_none()
        || !docxtpl_core::has_owned_callback_image_scope()
        || !crate::python_bridge::concrete_default_image(value)?
        || !crate::python_bridge::default_image_protocol(value, html)? {
        return Ok(None);
    }
    crate::inline_image::image_insert(value).map(Some)
}

fn soft_string<'py>(value: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    if value.is_instance(&value.py().get_type::<PyString>())? { Ok(value) }
    else if let Some(text) = native_default_image_string(&value, false)? { Ok(text) }
    else { Ok(value.str()?.into_any()) }
}

pub(crate) fn escape_string<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    match value.extract::<&str>() {
        Ok(text) => return match crate::python_bridge::escape_text(text) {
            std::borrow::Cow::Borrowed(_) => Ok(value.clone()),
            std::borrow::Cow::Owned(text) => Ok(PyString::new(py, &text).into_any()),
        },
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyUnicodeEncodeError>(py) => {},
        Err(error) => return Err(error),
    }
    // CPython strings may contain lone surrogates, which Rust str cannot hold.
    // Preserve the original object if no character needs escaping. Calling
    // PyUnicode_Replace on a subclass would otherwise return a new base str
    // even when the requested substring is absent.
    let length = unsafe { ffi::PyUnicode_GetLength(value.as_ptr()) };
    if length < 0 { return Err(PyErr::fetch(py)); }
    let mut needs_escape = false;
    for character in ['&', '<', '>', '\'', '"'] {
        let found = unsafe { ffi::PyUnicode_FindChar(value.as_ptr(), character as u32, 0, length, 1) };
        if found == -2 { return Err(PyErr::fetch(py)); }
        if found >= 0 { needs_escape = true; break; }
    }
    if !needs_escape { return Ok(value.clone()); }
    // Use Unicode primitives directly, bypassing subclass replace overrides.
    let mut escaped = value.clone();
    for (from, to) in [("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ("'", "&#39;"), ("\"", "&#34;")] {
        let from = PyString::new(py, from);
        let to = PyString::new(py, to);
        escaped = unsafe { Bound::from_owned_ptr_or_err(py,
            ffi::PyUnicode_Replace(escaped.as_ptr(), from.as_ptr(), to.as_ptr(), -1))? };
    }
    Ok(escaped)
}

/// Shared escape adapter for native concatenation and output formatting.
pub(crate) fn escape_value<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let markup = value.py().import("markupsafe")?.getattr("Markup")?;
    let escaped = if value.is_exact_instance_of::<PyString>() { escape_string(value)? }
        else if value.hasattr("__html__")? { value.call_method0("__html__")? }
        else { escape_string(value.str()?.as_any())? };
    // Markup remains the public Python string type; constructing that object
    // retains its __html__/__str__ protocols after native escaping decisions.
    markup.call1((escaped,))
}

fn escape_markup<'py>(value: &Bound<'py, PyAny>, result_type: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let result = escape_value(value)?;
    if result.getattr("__class__")?.is(result_type) { Ok(result) }
    else { result_type.call1((result,)) }
}

/// Native concatenation order and escaping, adapting caller-owned objects.
#[pyfunction]
pub(crate) fn python_concat<'py>(py: Python<'py>, sequence: &Bound<'py, PyAny>, autoescape: bool)
    -> PyResult<Bound<'py, PyAny>> {
    concat_iterator(py, sequence.try_iter()?, autoescape)
}

pub(crate) fn concat_values<'py>(py: Python<'py>, values: Vec<Bound<'py, PyAny>>, autoescape: bool)
    -> PyResult<Bound<'py, PyAny>> {
    concat_iterator(py, values.into_iter().map(Ok), autoescape)
}

fn concat_iterator<'py>(py: Python<'py>, mut iterator: impl Iterator<Item = PyResult<Bound<'py, PyAny>>>, autoescape: bool)
    -> PyResult<Bound<'py, PyAny>> {
    let mut converted = Vec::new();
    if !autoescape {
        for value in iterator {
            let value = value?;
            converted.push(match native_default_image_string(&value, false)? {
                Some(text) => text,
                None => value.str()?.into_any(),
            });
        }
        return join_strings(py, converted, None);
    }
    let mut html = false;
    for value in iterator.by_ref() {
        let value = soft_string(value?)?;
        html = value.hasattr("__html__")?;
        converted.push(value);
        if html { break; }
    }
    if !html { return join_strings(py, converted, None); }
    let separator = py.import("jinja2.runtime")?.getattr("Markup")?.call1(("",))?;
    let result_type = separator.getattr("__class__")?;
    let markup = py.import("markupsafe")?.getattr("Markup")?;
    let escape = separator.getattr("escape")?;
    let standard = markup.getattr("escape")?;
    // Inherited classmethods share their underlying function. User overrides
    // remain callbacks; only the standard algorithm is executed here in Rust.
    let method_type = py.import("types")?.getattr("MethodType")?;
    let standard_escape = escape.get_type().is(&method_type) && standard.get_type().is(&method_type)
        && escape.getattr("__func__")?.is(&standard.getattr("__func__")?);
    let escape_value = |value: &Bound<'py, PyAny>| {
        if standard_escape { escape_markup(value, &result_type) } else { escape.call1((value,)) }
    };
    let mut escaped = Vec::with_capacity(converted.len());
    // Escape the already-converted prefix before consuming the remainder;
    // an exception must leave later __str__ callbacks uncalled.
    // The prefix also owns temporary str subclasses through the final join.
    // Releasing them here could run __del__ before later conversions.
    for value in &converted { escaped.push(escape_value(value)?); }
    for value in iterator { escaped.push(escape_value(&soft_string(value?)?)?); }
    result_type.call1((join_strings(py, escaped, Some(&separator))?,))
}

#[pyfunction]
pub(crate) fn python_operator<'py>(
    py: Python<'py>,
    op: &str,
    left: &Bound<'py, PyAny>,
    right: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    if op == "contains" || op == "not_contains" {
        // The GIL is held and both objects remain alive for the C API call.
        let result = unsafe { ffi::PySequence_Contains(right.as_ptr(), left.as_ptr()) };
        if result < 0 {
            return Err(PyErr::fetch(py));
        }
        let value = (result != 0) ^ (op == "not_contains");
        return Ok(PyBool::new(py, value).to_owned().into_any());
    }
    if op == "slice" {
        let bounds = if right.is_exact_instance_of::<PyTuple>() {
            right.clone().cast_into::<PyTuple>()?
        } else {
            // Match argument expansion for callers using another iterable.
            unsafe { Bound::from_owned_ptr_or_err(py, ffi::PySequence_Tuple(right.as_ptr()))? }
                .cast_into::<PyTuple>()?
        };
        let slice = py.get_type::<PySlice>().call(&bounds, None)?;
        return left.get_item(slice);
    }
    // Each function returns a new owned reference or sets the Python error.
    // Rich comparisons intentionally retain non-bool result objects.
    let output = unsafe {
        match op {
            "add" => ffi::PyNumber_Add(left.as_ptr(), right.as_ptr()),
            "sub" => ffi::PyNumber_Subtract(left.as_ptr(), right.as_ptr()),
            "mul" => ffi::PyNumber_Multiply(left.as_ptr(), right.as_ptr()),
            "div" => ffi::PyNumber_TrueDivide(left.as_ptr(), right.as_ptr()),
            "int_div" => ffi::PyNumber_FloorDivide(left.as_ptr(), right.as_ptr()),
            "rem" => ffi::PyNumber_Remainder(left.as_ptr(), right.as_ptr()),
            "pow" => ffi::PyNumber_Power(left.as_ptr(), right.as_ptr(), py.None().as_ptr()),
            "neg" => ffi::PyNumber_Negative(left.as_ptr()),
            "pos" => ffi::PyNumber_Positive(left.as_ptr()),
            "==" => ffi::PyObject_RichCompare(left.as_ptr(), right.as_ptr(), ffi::Py_EQ),
            "!=" => ffi::PyObject_RichCompare(left.as_ptr(), right.as_ptr(), ffi::Py_NE),
            "<" => ffi::PyObject_RichCompare(left.as_ptr(), right.as_ptr(), ffi::Py_LT),
            "<=" => ffi::PyObject_RichCompare(left.as_ptr(), right.as_ptr(), ffi::Py_LE),
            ">" => ffi::PyObject_RichCompare(left.as_ptr(), right.as_ptr(), ffi::Py_GT),
            ">=" => ffi::PyObject_RichCompare(left.as_ptr(), right.as_ptr(), ffi::Py_GE),
            _ => return Err(PyKeyError::new_err(op.to_owned())),
        }
    };
    unsafe { Bound::from_owned_ptr_or_err(py, output) }
}
