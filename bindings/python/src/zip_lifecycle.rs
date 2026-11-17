//! Retain failed ZIP writes until the binding's traceback releases the writer.
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

#[derive(Default)]
pub(crate) struct IncompleteZip {
    records: Vec<Vec<u8>>,
    end: usize,
}

impl IncompleteZip {
    pub(crate) fn completed(&mut self, data: &[u8], central: usize, end: usize) -> PyResult<()> {
        let header = data
            .get(central..central + 46)
            .ok_or_else(|| crate::error("invalid ZIP central record"))?;
        let length = 46
            + [28, 30, 32]
                .into_iter()
                .map(|at| u16::from_le_bytes([header[at], header[at + 1]]) as usize)
                .sum::<usize>();
        let record = data
            .get(central..central + length)
            .ok_or_else(|| crate::error("truncated ZIP central record"))?;
        self.records.push(record.to_vec());
        self.end = end;
        Ok(())
    }

    fn finish(self, stream: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = stream.py();
        let size = self.records.iter().map(|record| record.len() as u64).sum();
        // Share the existing OPC writer's ZIP64 thresholds and footer encoding.
        let footer =
            docxtplrs::finish_opc_directory(self.records.len() as u64, size, self.end as u64)
                .map_err(crate::error)?;
        stream.call_method1("seek", (self.end,))?;
        for record in self.records {
            let name_end = 46 + u16::from_le_bytes([record[28], record[29]]) as usize;
            let extra_end = name_end + u16::from_le_bytes([record[30], record[31]]) as usize;
            // zipfile exposes each write, including empty extra/comment fields.
            for data in [
                &record[..46],
                &record[46..name_end],
                &record[name_end..extra_end],
                &record[extra_end..],
            ] {
                stream.call_method1("write", (PyBytes::new(py, data),))?;
            }
        }
        let end = if footer.starts_with(b"PK\x06\x06") {
            for data in [&footer[..56], &footer[56..76]] {
                stream.call_method1("write", (PyBytes::new(py, data),))?;
            }
            &footer[76..]
        } else {
            &footer[..]
        };
        stream.call_method1("write", (PyBytes::new(py, end),))?;
        stream.call_method1("write", (PyBytes::new(py, b""),))?;
        stream.call_method0("flush")?;
        Ok(())
    }
}

#[pyclass(module = "docxtpl_native._native")]
pub(crate) struct ZipTimestampWriter {
    source: Option<Py<PyBytes>>,
    stream: Option<Py<PyAny>>,
    pending: Option<IncompleteZip>,
    used: bool,
}

impl ZipTimestampWriter {
    fn finalize(&mut self, py: Python<'_>) {
        if let Some(pending) = self.pending.take() {
            if let Some(stream) = &self.stream {
                if let Err(error) = pending.finish(stream.bind(py)) {
                    error.write_unraisable(py, None);
                }
            }
        }
    }
}

#[pymethods]
impl ZipTimestampWriter {
    #[new]
    #[pyo3(signature = (source, stream=None))]
    fn new(source: Py<PyBytes>, stream: Option<Py<PyAny>>) -> Self {
        Self {
            source: Some(source),
            stream,
            pending: None,
            used: false,
        }
    }

    fn finish(&mut self, py: Python<'_>) -> PyResult<Option<Py<PyBytes>>> {
        if self.used {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "ZIP timestamp writer is already finished",
            ));
        }
        self.used = true;
        let source = self
            .source
            .as_ref()
            .ok_or_else(|| crate::error("ZIP timestamp writer was cleared"))?
            .clone_ref(py);
        let stream = self.stream.as_ref().map(|stream| stream.clone_ref(py));
        crate::zip_metadata::refresh_timestamps_inner(
            py,
            source.bind(py),
            stream.as_ref().map(|stream| stream.bind(py)),
            Some(&mut self.pending),
        )
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.source)?;
        visit.call(&self.stream)
    }

    fn __clear__(&mut self) {
        Python::attach(|py| self.finalize(py));
        self.source = None;
        self.stream = None;
    }
}

impl Drop for ZipTimestampWriter {
    fn drop(&mut self) {
        Python::try_attach(|py| self.finalize(py));
    }
}
