//! Inline image operations on the exposed Python document model.
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use pyo3::exceptions::PyNameError;

#[cfg(all(Py_3_11, not(Py_3_13), not(any(PyPy, GraalPy, Py_LIMITED_API))))]
unsafe extern "C" {
    fn _PyObject_GetMethod(object: *mut pyo3::ffi::PyObject, name: *mut pyo3::ffi::PyObject,
        method: *mut *mut pyo3::ffi::PyObject) -> std::ffi::c_int;
}

struct PythonImageStream<'py> { stream: Bound<'py, PyAny> }

impl docxtpl_core::image_stream::ImageStream for PythonImageStream<'_> {
    type Error = PyErr;

    fn seek(&mut self, offset: u64) -> PyResult<()> {
        self.stream.call_method1("seek", (offset,))?;
        Ok(())
    }

    fn read(&mut self, count: usize) -> PyResult<Vec<u8>> {
        self.stream.call_method1("read", (count,))?.extract()
    }

    fn tell(&mut self) -> PyResult<u64> {
        self.stream.call_method0("tell")?.extract()
    }
}

fn stream_image(image: &Bound<'_, PyAny>, descriptor: Bound<'_, PyAny>) -> PyResult<docxtplrs::template::Deferred> {
    let py = image.py();
    let width = image.getattr("width")?;
    let height = image.getattr("height")?;
    descriptor.call_method1("seek", (0,))?;
    let blob: Vec<u8> = descriptor.call_method0("read")?.extract()?;
    let mut stream = PythonImageStream { stream: descriptor };
    let info = docxtpl_core::image_stream::parse_header(&mut stream).map_err(|error| {
        use docxtpl_core::image_stream::ImageStreamError;
        match error {
            ImageStreamError::Source(error) => error,
            ImageStreamError::UnexpectedEnd | ImageStreamError::Unrecognized => {
                let name = if matches!(error, ImageStreamError::UnexpectedEnd) {
                    "UnexpectedEndOfFileError"
                } else { "UnrecognizedImageError" };
                match py.import("docx.image.exceptions").and_then(|module| module.getattr(name)).and_then(|class| class.call0()) {
                    Ok(value) => PyErr::from_value(value),
                    Err(error) => error,
                }
            }
            ImageStreamError::Invalid(message) => pyo3::exceptions::PyValueError::new_err(message),
        }
    })?;
    Ok(docxtplrs::template::Deferred::Image {
        blob: blob.into(), info: Some(info), filename: None,
        width: width.extract()?, height: height.extract()?,
        title: image.getattr("title")?.extract()?, descr: image.getattr("descr")?.extract()?,
        anchor: image.getattr("anchor")?.extract()?,
    })
}

fn deferred_image(image: &Bound<'_, PyAny>, descriptor: Bound<'_, PyAny>) -> PyResult<docxtplrs::template::Deferred> {
    if !descriptor.is_instance_of::<PyString>() { return stream_image(image, descriptor); }
    use base64::Engine as _;
    let py = image.py();
    let depth = pyo3::types::PyInt::new(py, 0);
    let encoded = crate::value_binding::encode_value(image, depth.as_any(), py.None().bind(py))?;
    let encoded = encoded.cast::<PyDict>()?;
    let get = |name: &str| encoded.get_item(name)?.ok_or_else(|| pyo3::exceptions::PyValueError::new_err("missing image field"));
    let data: String = get("data")?.extract()?;
    let blob = base64::engine::general_purpose::STANDARD.decode(data)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
    Ok(docxtplrs::template::Deferred::Image {
        blob: blob.into(), info: None, filename: get("filename")?.extract()?,
        width: get("width")?.extract()?, height: get("height")?.extract()?,
        anchor: get("anchor")?.extract()?, title: get("title")?.extract()?, descr: get("descr")?.extract()?,
    })
}

/// Check the native class or a subclass without invoking its instance protocols.
/// The unowned file path can postpone rejection only while both output
/// protocols still delegate to the native insertion guard.
pub(crate) fn unregistered_protocol_is_standard(image: &Bound<'_, PyAny>, package: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = image.py();
    let class = image.get_type();
    let original = package.getattr("_OriginalInlineImage")?.cast_into::<pyo3::types::PyType>()?;
    if unsafe { pyo3::ffi::PyType_IsSubtype(class.as_type_ptr(), original.as_type_ptr()) } == 0 {
        return Ok(false);
    }
    // A custom metaclass can change attribute lookup on the class.
    let meta = class.get_type();
    if !meta.is(&py.get_type::<pyo3::types::PyType>()) { return Ok(false); }
    // Check class dictionaries without resolving methods: a foreign descriptor
    // can execute even when accessed on the class.
    let mro = class.getattr("__mro__")?.cast_into::<pyo3::types::PyTuple>()?;
    let object = py.get_type::<PyString>().getattr("__base__")?;
    let mut found_original = false;
    for base in mro.iter() {
        let base_meta = base.get_type();
        if !base_meta.is(&meta) && !base_meta.is(&py.get_type::<pyo3::types::PyType>()) { return Ok(false); }
        let namespace = base.getattr("__dict__")?;
        // Even a class namespace may have str-subclass keys whose comparison
        // invokes user code. Check key types before membership lookups.
        for key in namespace.call_method0("keys")?.try_iter()? {
            if !key?.is_exact_instance_of::<PyString>() { return Ok(false); }
        }
        if !base.is(&object) && namespace.contains("__getattribute__")? { return Ok(false); }
        if base.is(&original) {
            found_original = true;
            for (name, saved, code) in [
                ("__str__", "_OriginalNativeImageStr", "_OriginalNativeImageStrCode"),
                ("__html__", "_OriginalNativeImageHtml", "_OriginalNativeImageHtmlCode"),
                ("_insert_image", "_OriginalNativeImageInsert", "_OriginalNativeImageInsertCode"),
            ] {
                if !namespace.contains(name)? { return Ok(false); }
                let method = namespace.get_item(name)?;
                if !method.is(&package.getattr(saved)?) || !method.getattr("__code__")?.is(&package.getattr(code)?) {
                    return Ok(false);
                }
            }
        } else if !found_original {
            for name in ["__str__", "__html__", "_insert_image"] {
                if namespace.contains(name)? { return Ok(false); }
            }
        }
    }
    if !found_original { return Ok(false); }
    unregistered_instance_protocol_is_standard(image, package)
}

#[cfg(all(Py_3_11, not(Py_3_13), not(any(PyPy, GraalPy, Py_LIMITED_API))))]
fn unregistered_instance_protocol_is_standard(image: &Bound<'_, PyAny>, package: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = image.py();
    let class = image.get_type();
    // CPython 3.11 and 3.12 place their managed dictionary state three words
    // before the object. Their layouts differ: 3.12 tags inline values in the
    // low bit; 3.11 stores the materialized dictionary pointer directly.
    // The flag check is required before reading this CPython preheader.
    let flags = unsafe { pyo3::ffi::PyType_GetFlags(class.as_type_ptr()) };
    if flags & pyo3::ffi::Py_TPFLAGS_MANAGED_DICT == 0 { return Ok(false); }
    let raw = unsafe { *image.as_ptr().cast::<usize>().sub(3) };
    let minor = py.version_info().minor;
    let dictionary = if minor == 11 {
        if raw & 1 != 0 { return Ok(false); }
        raw
    } else if minor == 12 {
        if raw & 1 != 0 { 0 } else { raw }
    } else { return Ok(false); };
    if dictionary != 0 {
        let dictionary = dictionary as *mut pyo3::ffi::PyObject;
        if unsafe { pyo3::ffi::PyDict_CheckExact(dictionary) } == 0 { return Ok(false); }
        let dictionary = unsafe { Bound::from_borrowed_ptr(py, dictionary) }.cast_into::<PyDict>()?;
        // A non-exact string key can run equality code during attribute lookup.
        if dictionary.iter().any(|(key, _)| !key.is_exact_instance_of::<PyString>()) { return Ok(false); }
    }
    for (name, original) in [("_insert_image", "_OriginalNativeImageInsert"), ("__html__", "_OriginalNativeImageHtml")] {
        let name = PyString::new(py, name);
        let mut method = std::ptr::null_mut();
        // _PyObject_GetMethod returns a new reference without allocating a
        // bound method for the standard function descriptor.
        let kind = unsafe { _PyObject_GetMethod(image.as_ptr(), name.as_ptr(), &mut method) };
        if kind < 0 { return Err(PyErr::fetch(py)); }
        let method = unsafe { Bound::from_owned_ptr_or_err(py, method)? };
        if kind != 1 || !method.is(&package.getattr(original)?) { return Ok(false); }
    }
    Ok(true)
}

#[cfg(not(all(Py_3_11, not(Py_3_13), not(any(PyPy, GraalPy, Py_LIMITED_API)))))]
fn unregistered_instance_protocol_is_standard(_image: &Bound<'_, PyAny>, _package: &Bound<'_, PyAny>) -> PyResult<bool> {
    // Other interpreters keep the conservative guard until their object layout
    // and method lookup are qualified.
    Ok(false)
}

/// Re-read a standard native image when a registered value enters a capture.
pub(crate) fn live_registered_image<'py>(image: &Bound<'py, PyAny>, html: bool) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = image.py();
    let identity = image.as_ptr() as usize;
    if !docxtpl_core::is_current_registered_image(identity) { return Ok(None); }
    let Some(package) = crate::find_loaded_module(py, pyo3::intern!(py, "docxtpl_native"))? else {
        return Ok(None);
    };
    let package = package.bind(py);
    let class = image.get_type();
    if !class.is(&package.getattr("_OriginalInlineImage")?) { return Ok(None); }
    if !class.get_type().is(&py.get_type::<pyo3::types::PyType>()) {
        return crate::python_format::stringify_value(image, html).map(Some);
    }
    let protocol = if html { "__html__" } else { "__str__" };
    let original = if html { "_OriginalNativeImageHtml" } else { "_OriginalNativeImageStr" };
    let original_code = if html { "_OriginalNativeImageHtmlCode" } else { "_OriginalNativeImageStrCode" };
    for (name, original, code) in [(protocol, original, original_code),
        ("_insert_image", "_OriginalNativeImageInsert", "_OriginalNativeImageInsertCode")] {
        let method = class.getattr(name)?;
        if !method.is(&package.getattr(original)?) || !method.getattr("__code__")?.is(&package.getattr(code)?) {
            return crate::python_format::stringify_value(image, html).map(Some);
        }
    }
    let object = py.get_type::<PyString>().getattr("__base__")?;
    if !class.getattr("__getattribute__")?.is(&object.getattr("__getattribute__")?) {
        return crate::python_format::stringify_value(image, html).map(Some);
    }
    let dictionary = unsafe { pyo3::ffi::PyObject_GenericGetDict(image.as_ptr(), std::ptr::null_mut()) };
    let dictionary = unsafe { Bound::from_owned_ptr_or_err(py, dictionary) }?.cast_into::<PyDict>()?;
    if dictionary.contains("_insert_image")? || (html && dictionary.contains("__html__")?) {
        return crate::python_format::stringify_value(image, html).map(Some);
    }
    let descriptor = image.getattr("image_descriptor")?;
    let deferred = deferred_image(image, descriptor)?;
    let result = docxtpl_core::materialize_callback_image(identity, deferred)
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("image scope changed during conversion"))?;
    Ok(Some(PyString::new(py, &result.map_err(crate::error)?).into_any()))
}

static IMAGE_GLOBALS: pyo3::sync::PyOnceLock<Py<PyDict>> = pyo3::sync::PyOnceLock::new();

pub(crate) fn capture_globals(py: Python<'_>) -> PyResult<()> {
    // The image class retains its function globals even when its module is
    // removed from sys.modules before the extension is loaded.
    if let Some(package) = super::find_loaded_module(py, pyo3::intern!(py, "docxtpl_native"))? {
        let globals = (|| -> PyResult<Bound<'_, PyDict>> {
            package.bind(py).getattr("_OriginalInlineImage")?.getattr("__init__")?
                .getattr("__globals__")?.cast_into::<PyDict>().map_err(Into::into)
        })();
        if let Ok(globals) = globals {
            let _ = IMAGE_GLOBALS.set(py, globals.unbind());
            return Ok(());
        }
    }
    if let Some(module) = super::find_loaded_module(py, pyo3::intern!(py, "docxtpl_native.inline_image"))? {
        let globals = module.bind(py).getattr("__dict__")?.cast_into::<PyDict>()?;
        let _ = IMAGE_GLOBALS.set(py, globals.unbind());
    }
    Ok(())
}

fn image_global<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    if let Some(globals) = IMAGE_GLOBALS.get(py) {
        if let Some(value) = globals.bind(py).get_item(name)? { return Ok(value); }
        if let Some(builtins) = globals.bind(py).get_item("__builtins__")? {
            if let Ok(builtins) = builtins.cast::<PyDict>() {
                if let Some(value) = builtins.get_item(name)? { return Ok(value); }
            } else if let Ok(value) = builtins.getattr(name) { return Ok(value); }
        }
    }
    Err(PyNameError::new_err(format!("name '{name}' is not defined")))
}

#[pyfunction]
pub(crate) fn image_init<'py>(
    image: &Bound<'py, PyAny>, tpl: &Bound<'py, PyAny>, descriptor: &Bound<'py, PyAny>,
    width: &Bound<'py, PyAny>, height: &Bound<'py, PyAny>, anchor: &Bound<'py, PyAny>,
    title: &Bound<'py, PyAny>, descr: &Bound<'py, PyAny>,
) -> PyResult<()> {
    image.setattr("tpl", tpl)?;
    image.setattr("image_descriptor", descriptor)?;
    image.setattr("width", width)?;
    image.setattr("height", height)?;
    image.setattr("anchor", anchor)?;
    image.setattr("title", if title.is_none() { title.clone() } else { image_global(image.py(), "str")?.call1((title,))? })?;
    image.setattr("descr", if descr.is_none() { descr.clone() } else { image_global(image.py(), "str")?.call1((descr,))? })?;
    Ok(())
}

#[pyfunction]
pub(crate) fn image_set_properties(image: &Bound<'_, PyAny>, run: &Bound<'_, PyAny>) -> PyResult<()> {
    let doc = run.call_method1("xpath", (".//wp:docPr",))?.get_item(0)?;
    let picture = run.call_method1("xpath", (".//pic:cNvPr",))?.get_item(0)?;
    for element in [doc, picture] {
        if !image.getattr("title")?.is_none() {
            let set = element.getattr("set")?;
            set.call1(("title", image.getattr("title")?))?;
        }
        if !image.getattr("descr")?.is_none() {
            let set = element.getattr("set")?;
            set.call1(("descr", image.getattr("descr")?))?;
        }
    }
    Ok(())
}

#[pyfunction]
pub(crate) fn image_add_hyperlink<'py>(run: &Bound<'py, PyAny>, url: &Bound<'py, PyAny>, part: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = run.py();
    let kwargs = PyDict::new(py);
    kwargs.set_item("is_external", true)?;
    let relationship = part.call_method("relate_to", (url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"), Some(&kwargs))?;
    let doc = run.call_method1("xpath", (".//wp:docPr",))?.get_item(0)?;
    let picture = run.call_method1("xpath", (".//pic:cNvPr",))?.get_item(0)?;
    let first = image_global(py, "OxmlElement")?.call1(("a:hlinkClick",))?;
    let set_first = first.getattr("set")?;
    set_first.call1((image_global(py, "qn")?.call1(("r:id",))?, &relationship))?;
    let second = image_global(py, "OxmlElement")?.call1(("a:hlinkClick",))?;
    let set_second = second.getattr("set")?;
    set_second.call1((image_global(py, "qn")?.call1(("r:id",))?, relationship))?;
    doc.call_method1("append", (first,))?;
    picture.call_method1("append", (second,))?;
    Ok(run.clone())
}

#[pyfunction]
pub(crate) fn image_insert<'py>(image: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = image.py();
    let identity = image.as_ptr() as usize;
    if docxtpl_core::has_unowned_callback_image_scope()
        && !docxtpl_core::is_current_registered_image(identity) {
        return Err(crate::python_bridge::unsupported(py,
            "XML/image values returned lazily need a native materialization hook"));
    }
    let owner = image.getattr(pyo3::intern!(py, "tpl"))?;
    if docxtpl_core::is_callback_image(identity, owner.as_ptr() as usize) {
        let descriptor = image.getattr("image_descriptor")?;
        let deferred = deferred_image(image, descriptor)?;
        let result = docxtpl_core::materialize_callback_image(identity, deferred)
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("image scope changed during conversion"))?;
        let xml = result.map_err(crate::error)?;
        return Ok(pyo3::types::PyString::new(py, &xml).into_any());
    }
    let part = owner.getattr(pyo3::intern!(py, "current_rendering_part"))?;
    let new_pic = part.getattr(pyo3::intern!(py, "new_pic_inline"))?;
    let mut pic = new_pic.call1((image.getattr(pyo3::intern!(py, "image_descriptor"))?, image.getattr(pyo3::intern!(py, "width"))?, image.getattr(pyo3::intern!(py, "height"))?))?;
    if !image.getattr(pyo3::intern!(py, "title"))?.is_none() || !image.getattr(pyo3::intern!(py, "descr"))?.is_none() {
        image.call_method1(pyo3::intern!(py, "_set_doc_properties"), (&pic,))?;
    }
    if image.getattr(pyo3::intern!(py, "anchor"))?.is_truthy()? && pic.call_method1(pyo3::intern!(py, "xpath"), (".//a:blip",))?.is_truthy()? {
        let add = image.getattr(pyo3::intern!(py, "_add_hyperlink"))?;
        pic = add.call1((&pic, image.getattr(pyo3::intern!(py, "anchor"))?, image.getattr(pyo3::intern!(py, "tpl"))?.getattr(pyo3::intern!(py, "current_rendering_part"))?))?;
    }
    let xml = pic.getattr(pyo3::intern!(py, "xml"))?;
    if xml.is_exact_instance_of::<PyString>() {
        let prefix = pyo3::intern!(py, "</w:t></w:r><w:r><w:drawing>");
        let suffix = pyo3::intern!(py, "</w:drawing></w:r><w:r><w:t xml:space=\"preserve\">");
        // SAFETY: %U receives three live Python Unicode objects with the GIL held.
        return unsafe { Bound::from_owned_ptr_or_err(py, pyo3::ffi::PyUnicode_FromFormat(
            c"%U%U%U".as_ptr(), prefix.as_ptr(), xml.as_ptr(), suffix.as_ptr())) };
    }
    let template = pyo3::intern!(py, "</w:t></w:r><w:r><w:drawing>%s</w:drawing></w:r><w:r><w:t xml:space=\"preserve\">");
    // Python's percent formatting also preserves arbitrary Python strings, including surrogates.
    unsafe { Bound::from_owned_ptr_or_err(py, pyo3::ffi::PyNumber_Remainder(template.as_ptr(), xml.as_ptr())) }
}
