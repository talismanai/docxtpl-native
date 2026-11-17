//! Obtain a borrowed libxml2 node through lxml's documented public C API.
//! The core serializer calls C directly; no lxml Python serializer is invoked.
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyType};

#[repr(C)]
struct Element {
    object: pyo3::ffi::PyObject,
    document: *mut std::ffi::c_void,
    node: *mut libxml::bindings::xmlNode,
    tag: *mut pyo3::ffi::PyObject,
}
type Root = unsafe extern "C" fn(*mut pyo3::ffi::PyObject) -> *mut Element;

#[repr(C)]
struct ElementTree {
    object: pyo3::ffi::PyObject,
    vtable: *mut std::ffi::c_void,
    document: *mut std::ffi::c_void,
    context: *mut Element,
}

struct Backend {
    serializer: docxtpl_core::foreign_xml::Serializer,
    element_type: Py<PyType>,
    tree_type: Py<PyType>,
}
static BACKEND: pyo3::sync::PyOnceLock<Backend> = pyo3::sync::PyOnceLock::new();
static ORIGINAL: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();

pub(crate) fn provider_path(
    module: &Bound<'_, PyAny>,
    address: *mut std::ffi::c_void,
) -> PyResult<std::path::PathBuf> {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        let mut info: libc::Dl_info = unsafe { std::mem::zeroed() };
        if unsafe { libc::dladdr(address, &mut info) } == 0 || info.dli_fname.is_null() {
            return Err(crate::python_bridge::unsupported(
                module.py(),
                "cannot identify the lxml C API provider",
            ));
        }
        let bytes = unsafe { std::ffi::CStr::from_ptr(info.dli_fname) }.to_bytes();
        Ok(std::path::PathBuf::from(std::ffi::OsStr::from_bytes(bytes)))
    }
    #[cfg(not(unix))]
    {
        let _ = address;
        module.getattr("__file__")?.extract()
    }
}

fn standard_serializer(module: &Bound<'_, PyAny>) -> PyResult<Option<Py<PyAny>>> {
    let function = module.getattr("tostring")?;
    if function.get_type().name()?.eq("cython_function_or_method")
        && function.getattr("__module__")?.eq("lxml.etree")?
        && function
            .getattr("__code__")?
            .getattr("co_name")?
            .eq("tostring")?
        && function
            .getattr("__code__")?
            .getattr("co_filename")?
            .eq("src/lxml/etree.pyx")?
    {
        Ok(Some(function.unbind()))
    } else {
        Ok(None)
    }
}

pub(crate) fn initialize(module: &Bound<'_, PyAny>) -> PyResult<()> {
    if ORIGINAL.get(module.py()).is_none() {
        if let Some(original) = standard_serializer(module)? {
            let _ = ORIGINAL.set(module.py(), original);
        }
    }
    Ok(())
}

pub(crate) fn serialize<'py>(
    module: &Bound<'py, PyAny>,
    tree: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = tree.py();
    let function = module.getattr("tostring")?;
    initialize(module)?;
    let original = ORIGINAL.get(py);
    // Explicit user substitutions remain binding callbacks. This path never
    // substitutes the reference serializer when a native operation fails.
    if !original.is_some_and(|original| function.is(original.bind(py))) {
        let options = PyDict::new(py);
        options.set_item("encoding", "unicode")?;
        options.set_item("pretty_print", false)?;
        return function.call((tree,), Some(&options));
    }
    let backend = BACKEND.get_or_try_init(py, || -> PyResult<Backend> {
        let version = module
            .getattr("LXML_VERSION")?
            .get_item(0)?
            .extract::<u32>()?;
        let libxml = module
            .getattr("LIBXML_VERSION")?
            .get_item(0)?
            .extract::<u32>()?;
        if version != 6 || libxml != 2 {
            return Err(crate::python_bridge::unsupported(
                py,
                "direct XML binding requires the lxml 6 / libxml2 2.x C ABI",
            ));
        }
        let capsule = module
            .getattr("__pyx_capi__")?
            .get_item("rootNodeOrRaise")?;
        let pointer = unsafe {
            pyo3::ffi::PyCapsule_GetPointer(
                capsule.as_ptr(),
                c"struct LxmlElement *(PyObject *)".as_ptr(),
            )
        };
        if pointer.is_null() {
            return Err(PyErr::fetch(py));
        }
        let path = provider_path(module, pointer)?;
        // The loaded lxml module is the owner/provider of this public API and
        // its exported libxml2 instance. Retain its library handle in the core.
        let serializer =
            unsafe { docxtpl_core::foreign_xml::Serializer::load(&path) }.map_err(|e| {
                crate::python_bridge::unsupported(
                    py,
                    &format!("direct XML backend unavailable: {e}"),
                )
            })?;
        let exported: Root = unsafe { serializer.symbol(b"rootNodeOrRaise\0") }
            .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
        if exported as *const () != pointer.cast_const().cast() {
            return Err(crate::python_bridge::unsupported(
                py,
                "XML library does not own the imported lxml C API",
            ));
        }
        // Import the actual exported C types, not mutable module attributes.
        let types = unsafe {
            let element: *mut pyo3::ffi::PyTypeObject = serializer
                .symbol(b"LxmlElementType\0")
                .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
            let tree: *mut pyo3::ffi::PyTypeObject = serializer
                .symbol(b"LxmlElementTreeType\0")
                .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
            if element.is_null()
                || tree.is_null()
                || (*element).tp_basicsize as usize != std::mem::size_of::<Element>()
                || (*tree).tp_basicsize as usize != std::mem::size_of::<ElementTree>()
            {
                return Err(crate::python_bridge::unsupported(
                    py,
                    "incompatible lxml C object layout",
                ));
            }
            (
                Bound::from_borrowed_ptr(py, element.cast())
                    .cast_into::<PyType>()?
                    .unbind(),
                Bound::from_borrowed_ptr(py, tree.cast())
                    .cast_into::<PyType>()?
                    .unbind(),
            )
        };
        Ok(Backend {
            serializer,
            element_type: types.0,
            tree_type: types.1,
        })
    })?;
    let (element, complete) = unsafe {
        if pyo3::ffi::PyObject_TypeCheck(tree.as_ptr(), backend.element_type.bind(py).as_type_ptr())
            != 0
        {
            (tree.as_ptr().cast::<Element>(), false)
        } else if pyo3::ffi::PyObject_TypeCheck(
            tree.as_ptr(),
            backend.tree_type.bind(py).as_type_ptr(),
        ) != 0
        {
            ((*(tree.as_ptr().cast::<ElementTree>())).context, true)
        } else {
            return Err(pyo3::exceptions::PyTypeError::new_err(format!(
                "Type '{}' cannot be serialized.",
                tree.get_type().name()?
            )));
        }
    };
    // Cython object fields use Py_None, not a null C pointer, when unset.
    if element.is_null() || element.cast::<pyo3::ffi::PyObject>() == py.None().as_ptr() {
        return Ok(py.None().into_bound(py));
    }
    // Public lxml C structs provide a borrowed node after checking the actual
    // C types. `tree` keeps the owner alive, including a tree's context element.
    // The GIL remains held and no Python callbacks run in the core.
    let bytes = unsafe {
        if complete {
            backend.serializer.serialize_document((*element).node)
        } else {
            backend.serializer.serialize((*element).node)
        }
    }
    .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
    let value = unsafe {
        Bound::from_owned_ptr_or_err(
            py,
            pyo3::ffi::PyUnicode_DecodeUTF8(
                bytes.as_ptr().cast(),
                bytes.len() as isize,
                std::ptr::null(),
            ),
        )?
    };
    Ok(value.cast_into::<PyString>()?.into_any())
}
