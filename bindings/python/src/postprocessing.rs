//! ZIP replacements through the caller's exposed file and mapping protocols.
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString};
use docxtplrs::package::{Entry, Package};
use std::sync::Arc;

fn bytes(value: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    let py = value.py();
    let mut view = pyo3::ffi::Py_buffer::new();
    if unsafe { pyo3::ffi::PyObject_GetBuffer(value.as_ptr(), &mut view, pyo3::ffi::PyBUF_SIMPLE) } != 0 {
        return Err(PyErr::fetch(py));
    }
    struct Guard(pyo3::ffi::Py_buffer);
    impl Drop for Guard { fn drop(&mut self) { unsafe { pyo3::ffi::PyBuffer_Release(&mut self.0) } } }
    let guard = Guard(view);
    if guard.0.len == 0 { return Ok(Vec::new()); }
    Ok(unsafe { std::slice::from_raw_parts(guard.0.buf.cast::<u8>(), guard.0.len as usize) }.to_vec())
}

fn replacement(value: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    let py = value.py();
    if value.is_instance(&py.get_type::<PyString>())? {
        // zipfile.writestr keeps this non-interned literal for every call.
        static UTF8: pyo3::sync::PyOnceLock<Py<PyString>> = pyo3::sync::PyOnceLock::new();
        let encoding = UTF8.get_or_init(py, || PyString::new(py, "utf-8").unbind());
        bytes(&value.call_method1(pyo3::intern!(py, "encode"), (encoding.bind(py),))?)
    } else { bytes(value) }
}

fn select(tpl: &Bound<'_, PyAny>, entry: &Entry) -> PyResult<Option<Vec<u8>>> {
    let py = tpl.py();
    // The reference reads the ZIP entry before looking up any replacement.
    let original = entry.bytes();
    let name = PyString::new(py, &entry.name);
    let names = tpl.getattr(pyo3::intern!(py, "zipname_to_replace"))?;
    if names.contains(&name)? {
        let value = tpl.getattr(pyo3::intern!(py, "zipname_to_replace"))?.get_item(&name)?;
        return replacement(&value).map(Some);
    }
    if entry.name.starts_with("word/media/") {
        let crc = docxtplrs::package::crc32(original).into_pyobject(py)?;
        let media = tpl.getattr(pyo3::intern!(py, "crc_to_new_media"))?;
        if media.contains(&crc)? {
            let value = tpl.getattr(pyo3::intern!(py, "crc_to_new_media"))?.get_item(&crc)?;
            return replacement(&value).map(Some);
        }
    } else if entry.name.starts_with("word/embeddings/") {
        let crc = docxtplrs::package::crc32(original).into_pyobject(py)?;
        let embedded = tpl.getattr(pyo3::intern!(py, "crc_to_new_embedded"))?;
        if embedded.contains(&crc)? {
            let value = tpl.getattr(pyo3::intern!(py, "crc_to_new_embedded"))?.get_item(&crc)?;
            return replacement(&value).map(Some);
        }
    }
    Ok(None)
}

fn zip_error(py: Python<'_>, error: String) -> PyErr {
    if error.contains("Could not find EOCD") {
        match py.import("zipfile").and_then(|module| module.getattr("BadZipFile"))
            .and_then(|class| class.call1(("File is not a zip file",))) {
            Ok(instance) => PyErr::from_value(instance),
            Err(error) => error,
        }
    } else { pyo3::exceptions::PyRuntimeError::new_err(error) }
}

fn load_package(py: Python<'_>, data: Vec<u8>) -> PyResult<Package> {
    Package::from_bytes_arc(Arc::from(data)).map_err(|error| zip_error(py, error))
}

// In a conventional ZIP, zipfile leaves the input at the end of the central
// directory when it cannot find [Content_Types].xml. ZIP64 needs its own
// locator handling and is intentionally not inferred by this scanner.
fn central_directory_end(data: &[u8]) -> Option<usize> {
    if data.len() < 22 { return None; }
    let first = data.len().saturating_sub(22 + u16::MAX as usize);
    (first..=data.len() - 22).rev().find(|&pos| {
        if &data[pos..pos + 4] != b"PK\x05\x06" { return false; }
        let comment = u16::from_le_bytes([data[pos + 20], data[pos + 21]]) as usize;
        if pos + 22 + comment > data.len() { return false; }
        !(pos >= 20 && &data[pos - 20..pos - 16] == b"PK\x06\x07")
    })
}

fn read_file<'py>(py: Python<'py>, path: &Bound<'py, PyAny>) -> PyResult<Vec<u8>> {
    let file = py.import("io")?.getattr(pyo3::intern!(py, "open"))?.call1((path, pyo3::intern!(py, "rb")))?;
    let result = file.call_method0(pyo3::intern!(py, "read")).and_then(|data| bytes(&data));
    let close = file.call_method0(pyo3::intern!(py, "close"));
    match (result, close) { (Ok(data), Ok(_)) => Ok(data), (Err(error), Ok(_)) => Err(error), (_, Err(error)) => Err(error) }
}

#[pyfunction]
pub(crate) fn post_process(tpl: &Bound<'_, PyAny>, file: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = tpl.py();
    if !tpl.getattr(pyo3::intern!(py, "crc_to_new_media"))?.is_truthy()?
        && !tpl.getattr(pyo3::intern!(py, "crc_to_new_embedded"))?.is_truthy()?
        && !tpl.getattr(pyo3::intern!(py, "zipname_to_replace"))?.is_truthy()? { return Ok(()); }
    let is_stream = file.hasattr(pyo3::intern!(py, "read"))?;
    let (mut package, temporary, original_attributes) = if is_stream {
        file.call_method1(pyo3::intern!(py, "seek"), (0,))?;
        let source = bytes(&file.call_method0(pyo3::intern!(py, "read"))?)?;
        let directory_end = central_directory_end(&source);
        let source: Arc<[u8]> = source.into();
        let loaded = Package::from_bytes_arc(source.clone()).map_err(|error| zip_error(py, error))?;
        let normalized = match docxtpl_core::opc::serialize_loaded(&loaded) {
            Ok(data) => data,
            Err(error) if error == "missing content types" => {
                if let Some(end) = directory_end { file.call_method1(pyo3::intern!(py, "seek"), (end,))?; }
                return Err(pyo3::exceptions::PyKeyError::new_err("There is no item named '[Content_Types].xml' in the archive"));
            }
            Err(error) => return Err(zip_error(py, error)),
        };
        let last = docxtpl_core::opc::last_loaded_part(&loaded).map_err(|error| zip_error(py, error))?;
        let mut archive = zip::ZipArchive::new(std::io::Cursor::new(source.as_ref())).map_err(crate::error)?;
        let entry = archive.by_name(&last).map_err(crate::error)?;
        let position = entry.data_start().checked_add(entry.compressed_size())
            .ok_or_else(|| crate::error("ZIP read cursor overflow"))?;
        file.call_method1(pyo3::intern!(py, "seek"), (position,))?;
        // The stream path saves an intermediate OPC package before truncating
        // the destination. Its clock callbacks and timestamp generation are
        // separate from the first save, including failure before replacement.
        let normalized = crate::zip_metadata::refresh_timestamps(py, &PyBytes::new(py, &normalized), None)?
            .expect("timestamp adapter returns bytes without a destination")
            .bind(py).as_bytes().to_vec();
        file.call_method1(pyo3::intern!(py, "seek"), (0,))?;
        file.call_method0(pyo3::intern!(py, "truncate"))?;
        file.call_method1(pyo3::intern!(py, "seek"), (0,))?;
        (load_package(py, normalized)?, None, None)
    } else {
        let temporary = PyString::new(py, &format!("{}_docxtpl_before_replace_medias", file.str()?.to_str()?)).into_any();
        py.import("os")?.call_method1(pyo3::intern!(py, "rename"), (file, &temporary))?;
        let source = read_file(py, &temporary)?;
        let attributes = crate::zip_metadata::attributes(&source).map_err(|error| zip_error(py, error))?;
        (load_package(py, source)?, Some(temporary), Some(attributes))
    };
    let output = if temporary.is_some() {
        Some(py.import("io")?.getattr(pyo3::intern!(py, "open"))?.call1((file, pyo3::intern!(py, "wb")))?)
    } else { None };
    let mut processed = 0usize;
    let transformed = docxtplrs::package::rewrite_entries(&mut package, |entry| {
        let result = select(tpl, entry);
        if result.is_ok() { processed += 1; }
        result
    });
    if let Err(error) = transformed {
        package.entries.truncate(processed);
        let mut partial = package.to_bytes_preserving_compression().map_err(|error| zip_error(py, error))?;
        crate::zip_metadata::postprocess_attributes(&mut partial, original_attributes.as_ref())
            .map_err(|error| zip_error(py, error))?;
        let target = output.as_ref().unwrap_or(file);
        let write = target.call_method1(pyo3::intern!(py, "write"), (PyBytes::new(py, &partial),));
        let close = output.as_ref().map(|out| out.call_method0(pyo3::intern!(py, "close")));
        write?;
        if let Some(result) = close { result?; }
        return Err(error);
    }
    let mut data = package.to_bytes_preserving_compression().map_err(|error| zip_error(py, error))?;
    crate::zip_metadata::postprocess_attributes(&mut data, original_attributes.as_ref())
        .map_err(|error| zip_error(py, error))?;
    if let Some(output) = output {
        let result = output.call_method1(pyo3::intern!(py, "write"), (PyBytes::new(py, &data),)).map(|_| ());
        let close = output.call_method0(pyo3::intern!(py, "close"));
        match (result, close) { (Err(error), _) => return Err(error), (_, Err(error)) => return Err(error), _ => {} }
        if let Some(path) = temporary { py.import("os")?.call_method1(pyo3::intern!(py, "remove"), (path,))?; }
    } else {
        file.call_method1(pyo3::intern!(py, "write"), (PyBytes::new(py, &data),))?;
    }
    if file.hasattr(pyo3::intern!(py, "read"))? { file.call_method1(pyo3::intern!(py, "seek"), (0,))?; }
    Ok(())
}
