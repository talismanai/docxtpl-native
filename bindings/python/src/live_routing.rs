//! Classify whether rendering must keep the exposed Python document graph live.
use std::collections::HashSet;
use pyo3::prelude::*;
use pyo3::types::{PyBool,PyBytes,PyDict,PyFloat,PyFrozenSet,PyInt,PyList,PyString,PyTuple,PyType};

/// Accept only exact data containers and scalars; cycles and aliases are safe.
#[pyfunction]
#[pyo3(signature = (context, compare_metaclasses=true))]
pub(crate) fn simple_context_names<'py>(context: &Bound<'py, PyAny>, compare_metaclasses: bool) -> PyResult<Option<Bound<'py, PyFrozenSet>>> {
    if !context.is_exact_instance_of::<PyDict>() { return Ok(None); }
    let mut pending=vec![context.clone()];
    let mut visited=HashSet::new();
    while let Some(value)=pending.pop() {
        if value.is_none() || value.is_exact_instance_of::<PyString>()
            || value.is_exact_instance_of::<PyInt>() || value.is_exact_instance_of::<PyBool>()
            || value.is_exact_instance_of::<PyFloat>() || value.is_exact_instance_of::<PyBytes>() {
            continue;
        }
        // Python's `type(value) in (str, int, ...)` can invoke equality on a
        // custom metaclass, including raising or returning true for a scalar.
        let kind=value.get_type();
        if compare_metaclasses && !kind.is_exact_instance_of::<PyType>() {
            let py=context.py();
            let leaves=PyTuple::new(py,[py.get_type::<PyString>(),py.get_type::<PyInt>(),
                py.get_type::<PyFloat>(),py.get_type::<PyBool>(),py.get_type::<PyBytes>(),
                py.None().bind(py).get_type()])?;
            // Equality on a metaclass can claim equivalence to a builtin while
            // the instance still runs arbitrary attribute/item callbacks.
            // Retain the comparisons and their side effects, but do not route
            // such an object through the closed data-only frontend.
            if leaves.contains(&kind)? { return Ok(None); }
        }
        if !visited.insert(value.as_ptr() as usize) { continue; }
        if value.is_exact_instance_of::<PyDict>() {
            for (key,item) in value.cast::<PyDict>()?.iter() {
                if !key.is_exact_instance_of::<PyString>() { return Ok(None); }
                pending.push(item);
            }
        } else if value.is_exact_instance_of::<PyList>() {
            pending.extend(value.cast::<PyList>()?.iter());
        } else if value.is_exact_instance_of::<PyTuple>() {
            pending.extend(value.cast::<PyTuple>()?.iter());
        } else if !plain_package_value(&value)? { return Ok(None); }
    }
    Ok(Some(PyFrozenSet::new(context.py(), context.cast::<PyDict>()?.keys().iter())?))
}

/// Whether `value` is an exact `RichText`, `RichTextParagraph` or `Listing`
/// whose class keeps its original `__str__` and `__html__` and whose only
/// attribute is an exact `xml` string: showing it runs no user code.
pub(crate) fn plain_package_value(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = value.py();
    let Some(package) = crate::find_loaded_module(py, pyo3::intern!(py, "docxtpl_native"))? else { return Ok(false); };
    let Ok(types) = package.bind(py).getattr(pyo3::intern!(py, "_PLAIN_VALUE_TYPES")) else { return Ok(false); };
    let kind = value.get_type();
    for entry in types.try_iter()? {
        let (class, show, html) = entry?.extract::<(Bound<'_, PyAny>, Bound<'_, PyAny>, Bound<'_, PyAny>)>()?;
        if !kind.is(&class) { continue; }
        if !class.getattr(pyo3::intern!(py, "__str__"))?.is(&show) || !class.getattr(pyo3::intern!(py, "__html__"))?.is(&html) {
            return Ok(false);
        }
        let Ok(attributes) = value.getattr(pyo3::intern!(py, "__dict__"))?.cast_into::<PyDict>() else { return Ok(false); };
        return Ok(attributes.len() == 1 && attributes.get_item("xml")?.is_some_and(|xml| xml.is_exact_instance_of::<PyString>()));
    }
    Ok(false)
}

// Inspect the real instance dictionary without invoking user attribute hooks.
// Programmatic subdocuments have a live document graph; file subdocuments do not.
fn is_programmatic_subdoc(value: &Bound<'_, PyAny>, class: Option<&Bound<'_, PyAny>>) -> PyResult<bool> {
    let Some(class) = class else { return Ok(false); };
    let Ok(class) = class.cast::<PyType>() else { return Ok(false); };
    if unsafe { pyo3::ffi::PyType_IsSubtype(value.get_type().as_type_ptr(), class.as_type_ptr()) } == 0 {
        return Ok(false);
    }
    has_subdoc_graph(value)
}

/// Only the internal document path may classify concrete special types without
/// invoking the user-visible `isinstance` protocol on every unrelated value.
/// A replaced class or instance checker keeps the original generic path.
pub(crate) fn default_special_types<'py>(package: &Bound<'py, PyAny>, image: &Bound<'py, PyAny>, subdoc: &Bound<'py, PyAny>)
    -> PyResult<Option<(Bound<'py, PyType>, Bound<'py, PyType>)>> {
    if !image.is(&package.getattr("_OriginalInlineImage")?) || !subdoc.is(&package.getattr("_OriginalSubdoc")?) {
        return Ok(None);
    }
    // Both classes use the plain `type` metaclass, so `isinstance` cannot be
    // customised for them; a subtype check gives the same answer.
    let plain = package.py().get_type::<PyType>();
    if !image.get_type().is(&plain) || !subdoc.get_type().is(&plain) { return Ok(None); }
    let Ok(image) = image.clone().cast_into::<PyType>() else { return Ok(None); };
    let Ok(subdoc) = subdoc.clone().cast_into::<PyType>() else { return Ok(None); };
    Ok(Some((image, subdoc)))
}

pub(crate) fn actual_special_type(value: &Bound<'_, PyAny>, classes: &(Bound<'_, PyType>, Bound<'_, PyType>)) -> bool {
    let actual = value.get_type();
    unsafe { pyo3::ffi::PyType_IsSubtype(actual.as_type_ptr(), classes.0.as_type_ptr()) != 0
        || pyo3::ffi::PyType_IsSubtype(actual.as_type_ptr(), classes.1.as_type_ptr()) != 0 }
}

pub(crate) fn has_subdoc_graph(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    let dict = unsafe { pyo3::ffi::PyObject_GenericGetDict(value.as_ptr(), std::ptr::null_mut()) };
    let dict = unsafe { Bound::from_owned_ptr_or_err(value.py(), dict) }?.cast_into::<PyDict>()?;
    dict.contains(pyo3::intern!(value.py(), "subdocx"))
}

fn needs_xml_recovery(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py=value.py();
    // CPython's Unicode API reads code points without requiring valid UTF-8,
    // so isolated surrogates remain valid inputs to the classifier.
    let length=unsafe { pyo3::ffi::PyUnicode_GetLength(value.as_ptr()) };
    if length<0 { return Err(PyErr::fetch(py)); }
    for index in 0..length {
        let ch=unsafe { pyo3::ffi::PyUnicode_ReadChar(value.as_ptr(),index) };
        if ch==u32::MAX { return Err(PyErr::fetch(py)); }
        if ch==b'<' as u32 || ch==b'&' as u32 || (ch<32 && !matches!(ch,9|10|13)) { return Ok(true); }
    }
    Ok(false)
}

#[pyfunction]
#[pyo3(signature = (context, environment, leaves, subdoc, programmatic_class=None))]
pub(crate) fn requires_live_model(context: &Bound<'_, PyAny>, environment: &Bound<'_, PyAny>, leaves: &Bound<'_, PyTuple>, subdoc: &Bound<'_, PyAny>, programmatic_class: Option<&Bound<'_, PyAny>>) -> PyResult<bool> {
    let mut pending=vec![context.clone()];
    let mut visited=HashSet::new();
    let mut observable=!environment.is_none();
    let mut native_files=false;
    while let Some(value)=pending.pop() {
        let kind=value.get_type();
        if value.is_exact_instance_of::<PyString>() {
            if needs_xml_recovery(&value)? { observable=true; }
            continue;
        }
        // This is an internal routing decision, not a Python equality test.
        // Metaclass __eq__ can have side effects or claim to be a scalar;
        // neither should turn an arbitrary object into a data-only leaf.
        if leaves.iter().any(|leaf| kind.is(&leaf)) { continue; }
        if !visited.insert(value.as_ptr() as usize) { continue; }
        if kind.is(subdoc) {
            if is_programmatic_subdoc(&value, programmatic_class)? { observable=true; }
            else { native_files=true; }
        } else if value.is_exact_instance_of::<PyDict>() {
            for (key,item) in value.cast::<PyDict>()?.iter() {
                if !key.is_exact_instance_of::<PyString>() { observable=true; }
                pending.push(item);
            }
        } else if value.is_exact_instance_of::<PyList>() {
            pending.extend(value.cast::<PyList>()?.iter());
        } else if value.is_exact_instance_of::<PyTuple>() {
            pending.extend(value.cast::<PyTuple>()?.iter());
        } else {
            observable=true;
        }
    }
    Ok(observable && !native_files)
}

#[pyfunction]
#[pyo3(signature = (context, globals, classes, convert, programmatic_class=None, image_class=None, actual_types_only=false))]
pub(crate) fn collect_specials(context: &Bound<'_, PyAny>, globals: &Bound<'_, PyAny>, classes: &Bound<'_, PyTuple>, convert: &Bound<'_, PyAny>, programmatic_class: Option<&Bound<'_, PyAny>>, image_class: Option<&Bound<'_, PyAny>>, actual_types_only: bool) -> PyResult<Py<PyList>> {
    let py=context.py();
    let concrete = if actual_types_only && classes.len() == 2 {
        default_special_types(&py.import("docxtpl_native")?.into_any(), &classes.get_item(0)?, &classes.get_item(1)?)?
    } else { None };
    let package = py.import("docxtpl_native")?;
    let lazy_images = actual_types_only && concrete.is_some()
        && convert.is(&package.getattr("_OriginalValue")?)
        && convert.getattr("__code__")?.is(&package.getattr("_OriginalValueCode")?);
    let mut pending=vec![context.clone(),globals.clone()];
    let mut visited=HashSet::new();
    let result=PyList::empty(py);
    while let Some(value)=pending.pop() {
        let identity=value.as_ptr() as usize;
        if !visited.insert(identity) { continue; }
        let special = match &concrete {
            Some(classes) => actual_special_type(&value, classes),
            None => value.is_instance(classes)?,
        };
        if special {
            if let (Some(subdoc_class), Some(image_class)) = (programmatic_class, image_class) {
                if classes.get_item(1)?.is(subdoc_class) && classes.get_item(0)?.is(image_class)
                    && is_programmatic_subdoc(&value, Some(subdoc_class))? { continue; }
            }
            let image = lazy_images && concrete.as_ref().is_some_and(|classes| unsafe {
                pyo3::ffi::PyType_IsSubtype(value.get_type().as_type_ptr(), classes.0.as_type_ptr()) != 0
            });
            let converted = if image {
                let pending = PyDict::new(py);
                pending.set_item("kind", "image")?;
                pending.set_item("data", "")?;
                pending.set_item("_lazy", true)?;
                pending.into_any()
            } else { convert.call1((&value,))? };
            let entry=PyDict::new(py);
            entry.set_item(pyo3::intern!(py,"identity"),identity)?;
            entry.set_item(pyo3::intern!(py,"value"),converted)?;
            result.append(entry)?;
        } else if value.is_exact_instance_of::<PyDict>() {
            pending.extend(value.cast::<PyDict>()?.iter().map(|(_,item)| item));
        } else if value.is_exact_instance_of::<PyList>() {
            pending.extend(value.cast::<PyList>()?.iter());
        } else if value.is_exact_instance_of::<PyTuple>() {
            pending.extend(value.cast::<PyTuple>()?.iter());
        }
    }
    Ok(result.unbind())
}
