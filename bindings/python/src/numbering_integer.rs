//! Python integer protocol for numbering identifiers.
use pyo3::prelude::*;
use pyo3::types::{PyInt, PyModule, PyString};

/// Convert a numbering identifier natively for the standard builtin, while
/// preserving an explicitly replaced Python `int` as a user callback.
pub(crate) fn parse_abstract_id(py: Python<'_>, value: &str) -> PyResult<i64> {
    let loaded = super::find_loaded_module(py, pyo3::intern!(py, "docxcompose.composer"))?;
    let integer = match loaded.as_ref() {
        Some(module) => super::pyglobals::global(&module.bind(py).cast::<PyModule>()?.dict(), pyo3::intern!(py, "int"))?,
        None => py.get_type::<PyInt>().into_any(),
    };
    if integer.is(&py.get_type::<PyInt>()) {
        return match value.trim().parse::<i64>() {
            Ok(value) => Ok(value),
            Err(error) if matches!(error.kind(), std::num::IntErrorKind::PosOverflow | std::num::IntErrorKind::NegOverflow) =>
                Err(pyo3::exceptions::PyOverflowError::new_err("numbering integer exceeds native i64 range")),
            Err(_) => {
                let repr = PyString::new(py, value).repr()?.extract::<String>()?;
                Err(pyo3::exceptions::PyValueError::new_err(format!("invalid literal for int() with base 10: {repr}")))
            }
        };
    }
    integer.call1((value,))?.extract()
}
