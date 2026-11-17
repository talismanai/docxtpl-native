//! `Document.save` of an exposed python-docx model, with python-docx's package
//! writer ported: the part walk, the content types, the relationship XML and
//! the ZIP archive are built here. Parts, relationships and their hooks
//! (`before_marshal`, `blob`, `rels`, partname and relationship properties)
//! stay Python objects, read in the order python-docx reads them. While any
//! class or global of the save chain was replaced, python-docx saves.
//!
//! The archive is written as the unexposed save writes its own (zipfile's
//! timestamps; per-record writes to a plain BytesIO), so only a path or a
//! plain BytesIO target is saved here; another stream observes zipfile's
//! writes and seeks, and python-docx saves to it. A path is created or
//! truncated where python-docx's ZipFile opens it; if a later hook fails it
//! is left empty, where python-docx leaves the members written so far.
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyString};

/// The archive python-docx would write for `document`, or None to let it save.
pub(crate) fn archive<'py>(document: &Bound<'py, PyAny>, target: &Bound<'py, PyAny>)
    -> PyResult<Option<Bound<'py, PyBytes>>> {
    let py = document.py();
    let path = if crate::zip_metadata::plain_bytesio(py, target)? { None }
        else if target.is_instance_of::<PyString>() || target.get_type().hasattr(pyo3::intern!(py, "__fspath__"))? {
            Some(py.import(pyo3::intern!(py, "os"))?.call_method1(pyo3::intern!(py, "fspath"), (target,))?)
        } else { return Ok(None); };
    let guard = py.import(pyo3::intern!(py, "docxtpl_native._save_guard"))?;
    if !guard.call_method1(pyo3::intern!(py, "save_chain_is_standard"), (document,))?.is_truthy()? {
        return Ok(None);
    }
    // Document.save: self._part.save(...); DocumentPart.save: self.package.save(...).
    let part = document.getattr(pyo3::intern!(py, "_part"))?;
    if !part.get_type().is(&guard.getattr(pyo3::intern!(py, "DOCUMENT_PART"))?) { return Ok(None); }
    let package = part.getattr(pyo3::intern!(py, "package"))?;
    if !guard.getattr(pyo3::intern!(py, "PACKAGES"))?.contains(package.get_type())? { return Ok(None); }
    let names = Names {
        packuri: guard.getattr(pyo3::intern!(py, "PACKURI"))?,
        relationships: guard.getattr(pyo3::intern!(py, "RELATIONSHIPS"))?,
    };
    // OpcPackage.save: before_marshal on self.parts, then
    // PackageWriter.write(pkg_file, self.rels, self.parts).
    for part in walk(&package)?.iter() {
        part.call_method0(pyo3::intern!(py, "before_marshal"))?;
    }
    let package_rels = package.getattr(pyo3::intern!(py, "rels"))?;
    let parts = walk(&package)?;
    // PhysPkgWriter(pkg_file): ZipFile opens a path for writing here.
    if let Some(path) = &path {
        py.import(pyo3::intern!(py, "io"))?.call_method1(pyo3::intern!(py, "open"), (path, "w+b"))?
            .call_method0(pyo3::intern!(py, "close"))?;
    }
    // _ContentTypesItem.from_parts, then its blob.
    let mut types = Vec::with_capacity(parts.len());
    for part in parts.iter() {
        let partname = part.getattr(pyo3::intern!(py, "partname"))?;
        let content_type = part.getattr(pyo3::intern!(py, "content_type"))?;
        let ext = names.ext(&partname)?;
        types.push((xml_text(&partname)?, ext, xml_text(&content_type)?));
    }
    let mut members = vec![("[Content_Types].xml".to_owned(),
        docxtpl_core::opc::content_types_xml(&types).map_err(crate::error)?)];
    members.push(("_rels/.rels".to_owned(), names.rels_xml(&package_rels)?));
    // _write_parts: each part's blob, then its relationships if it has any.
    for part in parts.iter() {
        let partname = part.getattr(pyo3::intern!(py, "partname"))?;
        let blob = blob_bytes(&part.getattr(pyo3::intern!(py, "blob"))?)?;
        members.push((names.membername(&partname)?, blob));
        if part.getattr(pyo3::intern!(py, "rels"))?.len()? > 0 {
            let rels_uri = names.rels_uri(&part.getattr(pyo3::intern!(py, "partname"))?)?;
            let xml = names.rels_xml(&part.getattr(pyo3::intern!(py, "rels"))?)?;
            members.push((names.membername(&rels_uri)?, xml));
        }
    }
    let data = docxtpl_core::opc::opc_archive(members).map_err(crate::error)?;
    Ok(Some(PyBytes::new(py, &data)))
}

/// OpcPackage.iter_parts: a depth-first walk of the relationship graph.
fn walk<'py>(package: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
    let visited = PyList::empty(package.py());
    visit(package, &visited)?;
    Ok(visited)
}

fn visit<'py>(source: &Bound<'py, PyAny>, visited: &Bound<'py, PyList>) -> PyResult<()> {
    let py = source.py();
    let values = source.getattr(pyo3::intern!(py, "rels"))?.call_method0(pyo3::intern!(py, "values"))?;
    for rel in values.try_iter()? {
        let rel = rel?;
        if rel.getattr(pyo3::intern!(py, "is_external"))?.is_truthy()? { continue; }
        let part = rel.getattr(pyo3::intern!(py, "target_part"))?;
        if visited.contains(&part)? { continue; }
        visited.append(&part)?;
        visit(&part, visited)?;
    }
    Ok(())
}

struct Names<'py> {
    packuri: Bound<'py, PyAny>,
    relationships: Bound<'py, PyAny>,
}

impl<'py> Names<'py> {
    /// An exact PackURI's text, whose properties the guard keeps original.
    fn plain(&self, uri: &Bound<'py, PyAny>) -> PyResult<Option<String>> {
        if uri.get_type().is(&self.packuri) { Ok(Some(uri.cast::<PyString>()?.to_str()?.to_owned())) } else { Ok(None) }
    }

    /// PackURI.ext: the extension posixpath.splitext gives, without the dot.
    fn ext(&self, uri: &Bound<'py, PyAny>) -> PyResult<String> {
        let Some(text) = self.plain(uri)? else { return xml_text(&uri.getattr("ext")?); };
        let filename = text.rsplit('/').next().unwrap_or("");
        Ok(match filename.rsplit_once('.') {
            Some((stem, ext)) if stem.chars().any(|c| c != '.') => ext.to_owned(),
            _ => String::new(),
        })
    }

    fn membername(&self, uri: &Bound<'py, PyAny>) -> PyResult<String> {
        match self.plain(uri)? {
            Some(text) => Ok(text.get(1..).unwrap_or("").to_owned()),
            None => uri.getattr("membername")?.extract(),
        }
    }

    /// PackURI.rels_uri: `<baseURI>/_rels/<filename>.rels`.
    fn rels_uri(&self, uri: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let Some(text) = self.plain(uri)? else { return uri.getattr("rels_uri"); };
        let (base, filename) = match text.rfind('/') {
            Some(index) => {
                let head = &text[..=index];
                // posixpath.split strips trailing slashes from a non-root head.
                let trimmed = head.trim_end_matches('/');
                (if trimmed.is_empty() { head.to_owned() } else { trimmed.to_owned() }, &text[index + 1..])
            }
            None => (String::new(), text.as_str()),
        };
        let separator = if base.is_empty() || base.ends_with('/') { "" } else { "/" };
        self.packuri.call1((format!("{base}{separator}_rels/{filename}.rels"),))
    }

    /// Relationships.xml.
    fn rels_xml(&self, rels: &Bound<'py, PyAny>) -> PyResult<Vec<u8>> {
        let py = rels.py();
        if !rels.get_type().is(&self.relationships) {
            return blob_bytes(&rels.getattr(pyo3::intern!(py, "xml"))?);
        }
        let mut entries = Vec::new();
        for rel in rels.call_method0(pyo3::intern!(py, "values"))?.try_iter()? {
            let rel = rel?;
            let id = xml_text(&rel.getattr(pyo3::intern!(py, "rId"))?)?;
            let kind = xml_text(&rel.getattr(pyo3::intern!(py, "reltype"))?)?;
            let target = xml_text(&rel.getattr(pyo3::intern!(py, "target_ref"))?)?;
            let external = rel.getattr(pyo3::intern!(py, "is_external"))?.is_truthy()?;
            entries.push((id, kind, target, external));
        }
        docxtpl_core::opc::relationships_xml(&entries).map_err(crate::error)
    }
}

/// An attribute value as lxml's `set` accepts it: text or UTF-8 bytes that
/// XML can hold.
fn xml_text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    let text = if let Ok(text) = value.cast::<PyString>() {
        text.to_str()?.to_owned()
    } else if let Ok(bytes) = value.cast::<PyBytes>() {
        String::from_utf8(bytes.as_bytes().to_vec())
            .map_err(|error| pyo3::exceptions::PyUnicodeDecodeError::new_err(error.to_string()))?
    } else {
        return Err(pyo3::exceptions::PyTypeError::new_err(format!(
            "Argument must be bytes or unicode, got '{}'", value.get_type().name()?)));
    };
    if text.chars().any(|c| (c < ' ' && !matches!(c, '\t' | '\n' | '\r')) || matches!(c, '\u{fffe}' | '\u{ffff}')) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "All strings must be XML compatible: Unicode or ASCII, no NULL bytes or control characters"));
    }
    Ok(text)
}

/// ZipFile.writestr's data: text is encoded as UTF-8, anything else must be
/// a bytes-like object.
fn blob_bytes(blob: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    if let Ok(text) = blob.cast::<PyString>() { return Ok(text.to_str()?.as_bytes().to_vec()); }
    let buffer = pyo3::buffer::PyBuffer::<u8>::get(blob)?;
    buffer.to_vec(blob.py())
}
