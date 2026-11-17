//! Exception boundaries for the Python-facing native renderer.
//! Python object identity and exception links are retained through PyO3.
use pyo3::exceptions::{PyRuntimeError, PyStopAsyncIteration, PyStopIteration};
use pyo3::prelude::*;

#[derive(Clone, Copy)]
pub(crate) enum Boundary {
    Generator,
    AsyncGenerator,
    Coroutine,
}

pub(crate) fn escaped_iteration(py: Python<'_>, original: PyErr, boundary: Boundary) -> PyErr {
    let message = if original.is_instance_of::<PyStopIteration>(py) {
        match boundary {
            Boundary::Generator => "generator raised StopIteration",
            Boundary::AsyncGenerator => "async generator raised StopIteration",
            Boundary::Coroutine => "coroutine raised StopIteration",
        }
    } else if matches!(boundary, Boundary::AsyncGenerator)
        && original.is_instance_of::<PyStopAsyncIteration>(py)
    {
        "async generator raised StopAsyncIteration"
    } else {
        return original;
    };
    let wrapped = PyRuntimeError::new_err(message);
    wrapped.set_context(py, Some(original.clone_ref(py)));
    wrapped.set_cause(py, Some(original));
    wrapped
}
