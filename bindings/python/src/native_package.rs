//! A document package read and changed natively, without the python-docx
//! model: parts, relationships and images. `DocxTemplate.open_package`
//! hands out a copy of the document and `commit_package` makes the document
//! those bytes, so only one mutable graph exists at a time.
use pyo3::exceptions::{PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

#[pyclass(unsendable, module = "docxtpl_native._native")]
pub(crate) struct NativePackage {
    package: docxtplrs::package::Package,
}

#[pymethods]
impl NativePackage {
    #[new]
    fn new(data: &[u8]) -> PyResult<Self> {
        let package = docxtplrs::package::Package::from_bytes(data).map_err(PyValueError::new_err)?;
        Ok(Self { package })
    }

    /// The names of the package's members, in archive order.
    fn names(&self) -> Vec<String> {
        self.package.entries.iter().map(|entry| entry.name.clone()).collect()
    }

    fn __contains__(&self, name: &str) -> bool {
        self.package.contains(name)
    }

    /// A member's bytes.
    fn read<'py>(&self, py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyBytes>> {
        match self.package.get(name) {
            Some(data) => Ok(PyBytes::new(py, data)),
            None => Err(PyKeyError::new_err(name.to_owned())),
        }
    }

    /// Replace a member's bytes, or add the member.
    fn write(&mut self, name: &str, data: &[u8]) -> PyResult<()> {
        if name.is_empty() || name.starts_with('/') || name.ends_with('/') {
            return Err(PyValueError::new_err(format!("invalid package member name: {name:?}")));
        }
        self.package.set(name, data.to_vec());
        Ok(())
    }

    /// A part's relationships as `(id, type, target, external)` tuples.
    fn relationships(&self, part: &str) -> Vec<(String, String, String, bool)> {
        self.package.rels(part).rels.iter()
            .map(|rel| (rel.id.clone(), rel.rel_type.clone(), rel.target.clone(), rel.is_external))
            .collect()
    }

    /// Add a relationship from `part` and return its new id.
    #[pyo3(signature = (part, rel_type, target, external=false))]
    fn relate(&mut self, part: &str, rel_type: &str, target: &str, external: bool) -> PyResult<String> {
        if !self.package.contains(part) {
            return Err(PyKeyError::new_err(part.to_owned()));
        }
        Ok(self.package.add_rel(part, rel_type, target, external))
    }

    /// Add an image under `word/media` (an identical image is reused), with
    /// its content type registered, and return its member name.
    fn add_image(&mut self, data: &[u8], extension: &str, content_type: &str) -> PyResult<String> {
        if extension.is_empty() || !extension.bytes().all(|byte| byte.is_ascii_alphanumeric()) {
            return Err(PyValueError::new_err(format!("invalid image extension: {extension:?}")));
        }
        let extension = extension.to_ascii_lowercase();
        Ok(self.package.get_or_add_image(data, &extension, content_type))
    }

    /// The package as a ZIP archive; unchanged members keep their compression.
    fn to_bytes<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let data = self.package.to_bytes_preserving_compression().map_err(PyValueError::new_err)?;
        Ok(PyBytes::new(py, &data))
    }
}
