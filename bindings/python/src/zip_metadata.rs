//! Compatibility for ZIP metadata emitted by Python's OPC package writer.
use std::io::Cursor;
use std::collections::HashMap;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyCFunction, PyDict, PyInt, PyList, PyModule, PySlice, PyTuple};

pub(crate) fn plain_bytesio(py: Python<'_>, stream: &Bound<'_, PyAny>) -> PyResult<bool> {
    // SAFETY: CPython owns this dictionary for the interpreter lifetime and
    // the GIL is held; this avoids invoking user import hooks.
    let modules = unsafe { Bound::from_borrowed_ptr(py, pyo3::ffi::PyImport_GetModuleDict()) };
    let Some(module) = modules.cast::<PyDict>()?.get_item("_io")? else { return Ok(false); };
    if !module.is_exact_instance_of::<PyModule>() { return Ok(false); }
    let Some(kind) = module.cast::<PyModule>()?.dict().get_item("BytesIO")? else { return Ok(false); };
    if !stream.get_type().is(&kind) { return Ok(false); }
    let instance = stream.getattr("__dict__")?;
    if !instance.is_exact_instance_of::<PyDict>() { return Ok(false); }
    let instance = instance.cast::<PyDict>()?;
    for name in ["tell", "write", "seek", "flush", "read", "seekable", "truncate", "close"] {
        if instance.contains(name)? { return Ok(false); }
    }
    Ok(true)
}

fn has_custom_clock(zipfile: &Bound<'_, PyAny>) -> PyResult<bool> {
    if !zipfile.is_exact_instance_of::<PyModule>() { return Ok(false); }
    let Some(clock) = zipfile.cast::<PyModule>()?.dict().get_item("time")? else { return Ok(false); };
    if !clock.is_exact_instance_of::<PyModule>() { return Ok(false); }
    let dict = clock.cast::<PyModule>()?.dict();
    for name in ["time", "localtime"] {
        let Some(function) = dict.get_item(name)? else { return Ok(false); };
        if !function.is_exact_instance_of::<PyCFunction>()
            || function.getattr("__module__")?.extract::<String>().ok().as_deref() != Some("time")
            || function.getattr("__name__")?.extract::<String>().ok().as_deref() != Some(name) {
            return Ok(true);
        }
    }
    Ok(false)
}

fn large_dos_fields<'py>(py: Python<'py>, fields: &[Bound<'py, PyAny>])
    -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    type Binary = unsafe extern "C" fn(*mut pyo3::ffi::PyObject, *mut pyo3::ffi::PyObject)
        -> *mut pyo3::ffi::PyObject;
    let op = |f: Binary, a: &Bound<'py, PyAny>, b: &Bound<'py, PyAny>| unsafe {
        Bound::from_owned_ptr_or_err(py, f(a.as_ptr(), b.as_ptr()))
    };
    let number = |n: i32| -> PyResult<Bound<'py, PyAny>> { Ok(n.into_pyobject(py)?.into_any()) };
    let year = op(pyo3::ffi::PyNumber_Subtract, &fields[0], &number(1980)?)?;
    let year = op(pyo3::ffi::PyNumber_Lshift, &year, &number(9)?)?;
    let month = op(pyo3::ffi::PyNumber_Lshift, &fields[1], &number(5)?)?;
    let date = op(pyo3::ffi::PyNumber_Or, &year, &month)?;
    let date = op(pyo3::ffi::PyNumber_Or, &date, &fields[2])?;
    let hour = op(pyo3::ffi::PyNumber_Lshift, &fields[3], &number(11)?)?;
    let minute = op(pyo3::ffi::PyNumber_Lshift, &fields[4], &number(5)?)?;
    let second = op(pyo3::ffi::PyNumber_FloorDivide, &fields[5], &number(2)?)?;
    let time = op(pyo3::ffi::PyNumber_Or, &hour, &minute)?;
    let time = op(pyo3::ffi::PyNumber_Or, &time, &second)?;
    Ok((date, time))
}

#[pyfunction]
#[pyo3(signature = (source, stream=None))]
pub(crate) fn refresh_timestamps<'py>(py: Python<'py>, source: &Bound<'py, PyBytes>,
    stream: Option<&Bound<'py, PyAny>>) -> PyResult<Option<Py<PyBytes>>> {
    refresh_timestamps_inner(py, source, stream, None)
}

pub(crate) fn refresh_timestamps_inner<'py>(py: Python<'py>, source: &Bound<'py, PyBytes>,
    stream: Option<&Bound<'py, PyAny>>, mut pending: Option<&mut Option<crate::zip_lifecycle::IncompleteZip>>)
    -> PyResult<Option<Py<PyBytes>>> {
    let zipfile = py.import("zipfile")?;
    let data = source.as_bytes();
    let positions = {
        let mut archive = zip::ZipArchive::new(Cursor::new(data)).map_err(crate::error)?;
        let mut positions = Vec::with_capacity(archive.len());
        for index in 0..archive.len() {
            let entry = archive.by_index_raw(index).map_err(crate::error)?;
            let central = checked_offset(data, entry.central_header_start()).map_err(crate::error)?;
            let local = usize::try_from(entry.header_start()).map_err(crate::error)?;
            if data.get(local..local.saturating_add(4)) != Some(b"PK\x03\x04")
                || local.checked_add(14).is_none_or(|end| end > data.len()) {
                return Err(crate::error("invalid local ZIP header"));
            }
            positions.push((central, local));
        }
        positions
    };
    let central_start = positions.iter().map(|(central, _)| *central).min().unwrap_or(data.len());
    let ordered = positions.first().is_some_and(|(_, local)| *local == 0)
        && positions.windows(2).all(|pair| pair[0].1 < pair[1].1)
        && positions.last().is_some_and(|(_, local)| *local < central_start);
    let streaming = if let Some(stream) = stream {
        if plain_bytesio(py, stream)? {
            // BytesIO.tell() also matches the reference's early error for a closed stream.
            let at_start = stream.call_method0("tell")?.extract::<u64>()? == 0;
            at_start && ordered && has_custom_clock(&zipfile)?
        } else { false }
    } else { false };
    if streaming {
        if let Some(pending) = pending.as_deref_mut() { *pending = Some(Default::default()); }
    }
    let mut streamed = streaming.then(|| data.to_vec());
    let mut changes = Vec::with_capacity(positions.len());
    for (index, &(central, local_header)) in positions.iter().enumerate() {
        let local = zipfile.getattr("time")?.getattr("localtime")?;
        let instant = zipfile.getattr("time")?.getattr("time")?.call0()?;
        let slice = py.get_type::<PySlice>().call1((py.None(), 6, py.None()))?;
        let date = local.call1((instant,))?.get_item(slice)?;
        if !date.is_exact_instance_of::<PyTuple>() && !date.is_exact_instance_of::<PyList>() {
            return Err(crate::python_bridge::unsupported(py, "custom ZIP timestamp needs a native save adapter"));
        }
        let mut fields = [0i64; 6];
        let mut objects = Vec::with_capacity(6);
        let mut large = false;
        for (index, field) in fields.iter_mut().enumerate() {
            let value = date.get_item(index)?;
            if !value.is_exact_instance_of::<PyInt>() && !value.is_exact_instance_of::<PyBool>() {
                return Err(crate::python_bridge::unsupported(py, "custom ZIP timestamp needs a native save adapter"));
            }
            match value.extract::<i64>() {
                Ok(number) => {
                    if index == 0 && number < 1980 {
                        return Err(pyo3::exceptions::PyValueError::new_err("ZIP does not support timestamps before 1980"));
                    }
                    *field = number;
                }
                Err(_) => {
                    if index == 0 && value.lt(1980)? {
                        return Err(pyo3::exceptions::PyValueError::new_err("ZIP does not support timestamps before 1980"));
                    }
                    large = true;
                }
            }
            objects.push(value);
        }
        let [year, month, day, hour, minute, second] = fields;
        let dos_date = ((i128::from(year) - 1980) << 9) | (i128::from(month) << 5) | i128::from(day);
        let dos_time = (i128::from(hour) << 11) | (i128::from(minute) << 5) | i128::from(second).div_euclid(2);
        // Obtain CPython's real struct.error on the exceptional path. The
        // public struct.error attribute is mutable and its text differs across
        // Python versions. The reference packs using the same native function.
        let out_of_range = |time: &Bound<'_, PyAny>, date: &Bound<'_, PyAny>| -> PyErr {
            let result = (|| -> PyResult<()> {
                // SAFETY: CPython owns the module dictionary; the GIL is held.
                let modules = unsafe { Bound::from_borrowed_ptr(py, pyo3::ffi::PyImport_GetModuleDict()) };
                let module = modules.cast::<PyDict>()?.get_item("_struct")?
                    .ok_or_else(|| crate::python_bridge::unsupported(py, "custom ZIP packing needs a native save adapter"))?;
                if !module.is_exact_instance_of::<PyModule>() {
                    return Err(crate::python_bridge::unsupported(py, "custom ZIP packing needs a native save adapter"));
                }
                let pack = module.cast::<PyModule>()?.dict().get_item("pack")?
                    .ok_or_else(|| crate::python_bridge::unsupported(py, "custom ZIP packing needs a native save adapter"))?;
                if !pack.is_exact_instance_of::<PyCFunction>() || !zipfile.getattr("struct")?.getattr("pack")?.is(&pack)
                    || pack.getattr("__module__")?.extract::<String>().ok().as_deref() != Some("_struct")
                    || pack.getattr("__name__")?.extract::<String>().ok().as_deref() != Some("pack") {
                    return Err(crate::python_bridge::unsupported(py, "custom ZIP packing needs a native save adapter"));
                }
                pack.call1(("<HH", time, date))?;
                Ok(())
            })();
            match result {
                Err(error) => error,
                Ok(()) => crate::python_bridge::unsupported(py, "custom ZIP packing needs a native save adapter"),
            }
        };
        let (dos_date, dos_time) = if large {
            let (date, time) = large_dos_fields(py, &objects)?;
            match (date.extract::<u16>(), time.extract::<u16>()) {
                (Ok(date), Ok(time)) => (date, time),
                _ => return Err(out_of_range(&time, &date)),
            }
        } else {
            match (u16::try_from(dos_date), u16::try_from(dos_time)) {
                (Ok(date), Ok(time)) => (date, time),
                _ => {
                    let date = dos_date.into_pyobject(py)?.into_any();
                    let time = dos_time.into_pyobject(py)?.into_any();
                    return Err(out_of_range(&time, &date));
                }
            }
        };
        if let Some(bytes) = &mut streamed {
            let stream = stream.unwrap();
            if !plain_bytesio(py, stream)? {
                if let Some(pending) = pending.as_deref_mut() { *pending = None; }
                return Err(crate::python_bridge::unsupported(py, "custom ZIP stream needs a native save adapter"));
            }
            // zipfile seeks to the next local-header position after evaluating
            // the clock, even if that callback moved or modified the stream.
            stream.call_method1("seek", (local_header,))?;
            bytes[central + 12..central + 14].copy_from_slice(&dos_time.to_le_bytes());
            bytes[central + 14..central + 16].copy_from_slice(&dos_date.to_le_bytes());
            bytes[local_header + 10..local_header + 12].copy_from_slice(&dos_time.to_le_bytes());
            bytes[local_header + 12..local_header + 14].copy_from_slice(&dos_date.to_le_bytes());
            let end = positions.get(index + 1).map_or(central_start, |(_, local)| *local);
            stream.call_method1("write", (PyBytes::new(py, &bytes[local_header..end]),))?;
            if let Some(Some(pending)) = pending.as_deref_mut() { pending.completed(bytes, central, end)?; }
        } else { changes.push((central, local_header, dos_date, dos_time)); }
    }
    if let Some(bytes) = streamed {
        // ZipFile.close marks the writer closed even if its final writes fail.
        if let Some(pending) = pending.as_deref_mut() { *pending = None; }
        let stream = stream.unwrap();
        stream.call_method1("write", (PyBytes::new(py, &bytes[central_start..]),))?;
        stream.call_method0("flush")?;
        return Ok(None);
    }
    if changes.iter().all(|(central, local, date, time)| {
        data[*central + 12..*central + 14] == time.to_le_bytes()
            && data[*central + 14..*central + 16] == date.to_le_bytes()
            && data[*local + 10..*local + 12] == time.to_le_bytes()
            && data[*local + 12..*local + 14] == date.to_le_bytes()
    }) { return Ok(Some(source.clone().unbind())); }
    Ok(Some(PyBytes::new_with(py, data.len(), |result| {
        result.copy_from_slice(data);
        for (central, local, date, time) in changes {
            result[central + 12..central + 14].copy_from_slice(&time.to_le_bytes());
            result[central + 14..central + 16].copy_from_slice(&date.to_le_bytes());
            result[local + 10..local + 12].copy_from_slice(&time.to_le_bytes());
            result[local + 12..local + 14].copy_from_slice(&date.to_le_bytes());
        }
        Ok(())
    })?.unbind()))
}

fn checked_offset(bytes: &[u8], offset: u64) -> Result<usize, String> {
    let offset = usize::try_from(offset).map_err(|e| e.to_string())?;
    if bytes.get(offset..offset.saturating_add(4)) != Some(b"PK\x01\x02")
        || offset.checked_add(42).is_none_or(|end| end > bytes.len()) {
        return Err("invalid central directory header".to_owned());
    }
    Ok(offset)
}

fn entries(bytes: &[u8]) -> Result<Vec<(String, usize, u16, u32)>, String> {
    let mut archive = zip::ZipArchive::new(Cursor::new(bytes)).map_err(|e| e.to_string())?;
    let mut result = Vec::with_capacity(archive.len());
    for index in 0..archive.len() {
        let entry = archive.by_index_raw(index).map_err(|e| e.to_string())?;
        let offset = checked_offset(bytes, entry.central_header_start())?;
        let creator = u16::from_le_bytes(bytes[offset + 4..offset + 6].try_into().unwrap());
        let attr = u32::from_le_bytes(bytes[offset + 38..offset + 42].try_into().unwrap());
        result.push((entry.name().to_owned(), offset, creator, attr));
    }
    Ok(result)
}

pub(crate) fn attributes(bytes: &[u8]) -> Result<HashMap<String, (u16, u32)>, String> {
    Ok(entries(bytes)?.into_iter().map(|(name, _, creator, attr)| (name, (creator, attr))).collect())
}

pub(crate) fn restore_attributes(bytes: &mut [u8], original: &HashMap<String, (u16, u32)>) -> Result<(), String> {
    let mut changes = Vec::new();
    for (name, offset, _, _) in entries(bytes)? {
        let (creator, attr) = *original.get(&name).ok_or_else(|| format!("missing ZIP attributes for {name}"))?;
        // zipfile.writestr substitutes its default for a zero attribute.
        changes.push((offset, creator, if attr == 0 { 0o600_u32 << 16 } else { attr }));
    }
    for (offset, creator, attr) in changes {
        bytes[offset + 4..offset + 6].copy_from_slice(&creator.to_le_bytes());
        bytes[offset + 38..offset + 42].copy_from_slice(&attr.to_le_bytes());
    }
    Ok(())
}

pub(crate) fn postprocess_attributes(bytes: &mut [u8], original: Option<&HashMap<String, (u16, u32)>>) -> Result<(), String> {
    match original {
        Some(attributes) => restore_attributes(bytes, attributes),
        None => normalize_permissions(bytes),
    }
}

/// Python's `zipfile.writestr(name, data)` assigns mode 0o600 without Unix
/// file-type bits. The ZIP writer used by the native core adds S_IFREG and
/// 0o644 instead. Patch only central-directory attributes, leaving compressed
/// content, timestamps, ordering, and local headers untouched.
pub(crate) fn normalize_permissions(bytes: &mut [u8]) -> Result<(), String> {
    let offsets = {
        let mut archive = zip::ZipArchive::new(Cursor::new(&*bytes)).map_err(|e| e.to_string())?;
        let mut offsets = Vec::with_capacity(archive.len());
        for index in 0..archive.len() {
            let entry = archive.by_index_raw(index).map_err(|e| e.to_string())?;
            offsets.push(checked_offset(bytes, entry.central_header_start())?);
        }
        offsets
    };
    for offset in offsets {
        bytes[offset + 38..offset + 42].copy_from_slice(&(0o600_u32 << 16).to_le_bytes());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};

    #[test]
    fn updates_modes_without_changing_payload_or_compression() {
        let mut writer = zip::ZipWriter::new(Cursor::new(Vec::new()));
        for (name, content, method) in [("one.xml", &b"<x/>"[..], zip::CompressionMethod::Deflated),
            ("two.bin", &b"\0\xff"[..], zip::CompressionMethod::Stored)] {
            writer.start_file(name, zip::write::SimpleFileOptions::default().compression_method(method)).unwrap();
            writer.write_all(content).unwrap();
        }
        let mut bytes = writer.finish().unwrap().into_inner();
        normalize_permissions(&mut bytes).unwrap();
        let mut archive = zip::ZipArchive::new(Cursor::new(bytes)).unwrap();
        for (name, expected, method) in [("one.xml", &b"<x/>"[..], zip::CompressionMethod::Deflated),
            ("two.bin", &b"\0\xff"[..], zip::CompressionMethod::Stored)] {
            let mut file = archive.by_name(name).unwrap();
            assert_eq!(file.unix_mode(), Some(0o600));
            assert_eq!(file.compression(), method);
            let mut actual = Vec::new();
            file.read_to_end(&mut actual).unwrap();
            assert_eq!(actual, expected);
        }
    }

    #[test]
    fn invalid_archive_is_rejected_without_mutation() {
        let mut bytes = b"invalid zip".to_vec();
        let original = bytes.clone();
        assert!(normalize_permissions(&mut bytes).is_err());
        assert_eq!(bytes, original);
    }
}
