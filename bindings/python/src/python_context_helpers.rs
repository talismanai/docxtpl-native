//! Context derivation using the existing foreign-object constructors.
use crate::pyglobals::{FrameGlobals, frame_global};
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyIterator};

fn receiver<'py>(
    py: Python<'py>,
    method: &Bound<'py, PyAny>,
    function_key: &str,
    code_key: &str,
    name: &str,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let binding = crate::python_function::binding(py)?;
    if method.get_type().is(&binding.getattr("_METHOD_TYPE")?)
        && crate::python_function::is_standard_function(
            &method.getattr("__func__")?,
            &binding.getattr(function_key)?,
            &binding.getattr(code_key)?,
            binding.getattr("_RUNTIME_MODULE")?.cast()?,
            name,
        )?
    {
        Ok(Some(method.getattr("__self__")?))
    } else {
        Ok(None)
    }
}

/// Execute the standard get_exported body without choosing a Python method.
/// The caller remains responsible for honoring an overridden method.
#[pyfunction]
pub(crate) fn python_context_exported_body<'py>(
    py: Python<'py>,
    context: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let iterator = context.getattr("exported_vars")?.try_iter()?;
    let result = PyDict::new(py);
    for key in iterator {
        let key = key?;
        // The comprehension evaluates the output key before the subscript.
        // Both operands stay alive while the mapping's lookup protocol runs.
        let output_key = key.clone();
        let subscript = key.clone();
        let mapping = context.getattr("vars")?;
        let value = mapping.get_item(&subscript)?;
        drop(subscript);
        drop(mapping);
        result.set_item(output_key, value)?;
    }
    Ok(result)
}

pub(crate) fn get_exported<'py>(py: Python<'py>, context: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let method = context.getattr("get_exported")?;
    if let Some(context) = receiver(py, &method, "_CONTEXT_GET_EXPORTED", "_CONTEXT_GET_EXPORTED_CODE", "get_exported")? {
        python_context_exported_body(py, &context).map(Bound::into_any)
    } else { method.call0() }
}

#[pyfunction]
pub(crate) fn python_context_get_all<'py>(
    py: Python<'py>,
    context: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let method = context.getattr("get_all")?;
    let Some(context) = receiver(
        py,
        &method,
        "_CONTEXT_GET_ALL",
        "_CONTEXT_GET_ALL_CODE",
        "get_all",
    )?
    else {
        return method.call0();
    };
    if !context.getattr("vars")?.is_truthy()? {
        return context.getattr("parent");
    }
    if !context.getattr("parent")?.is_truthy()? {
        return context.getattr("vars");
    }
    let scope = FrameGlobals::of_function(&method.getattr("__func__")?)?;
    let constructor = frame_global(&scope, pyo3::intern!(py, "dict"))?;
    let parent = context.getattr("parent")?;
    let variables = context.getattr("vars")?;
    // **mapping expansion is a CPython call protocol, including duplicate keys
    // and custom mapping failures. The binding performs only that call shape.
    crate::python_function::binding(py)?
        .getattr("_call_with_mapping")?
        .call1((constructor, parent, variables))
}

pub(crate) fn resolve<'py>(py: Python<'py>, context: &Bound<'py, PyAny>, method: &Bound<'py, PyAny>, key: &Bound<'py, PyAny>) -> PyResult<(Option<Bound<'py, PyAny>>, bool)> {
    let standard = receiver(py, &method, "_CONTEXT_RESOLVE", "_CONTEXT_RESOLVE_CODE", "resolve_or_missing")?;
    let value = if let Some(context) = &standard {
        if context.getattr("vars")?.contains(key)? { context.getattr("vars")?.get_item(key)? }
        else if context.getattr("parent")?.contains(key)? { context.getattr("parent")?.get_item(key)? }
        else {
            let scope = FrameGlobals::of_function(&method.getattr("__func__")?)?;
            frame_global(&scope, pyo3::intern!(py, "missing"))?
        }
    } else { method.call1((key,))? };
    let missing = crate::python_function::binding(py)?.getattr("_RUNTIME_MODULE")?.getattr("missing")?;
    Ok(((!value.is(&missing)).then_some(value), standard.as_ref().is_some_and(|receiver| receiver.is(context))))
}

pub(crate) fn new_context<'py>(
    py: Python<'py>,
    constructor: &Bound<'py, PyAny>,
    environment: &Bound<'py, PyAny>,
    name: &Bound<'py, PyAny>,
    blocks: &Bound<'py, PyAny>,
    variables: &Bound<'py, PyAny>,
    shared: bool,
    globals: &Bound<'py, PyAny>,
    locals: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let binding = crate::python_function::binding(py)?;
    if !crate::python_function::is_standard_function(
        constructor,
        &binding.getattr("_RUNTIME_NEW_CONTEXT")?,
        &binding.getattr("_RUNTIME_NEW_CONTEXT_CODE")?,
        binding.getattr("_RUNTIME_MODULE")?.cast()?,
        "new_context",
    )? {
        return constructor.call1((
            environment,
            name,
            blocks,
            variables,
            shared,
            globals,
            locals,
        ));
    }
    let scope = FrameGlobals::of_function(constructor)?;
    let mut parent = if variables.is_none() {
        PyDict::new(py).into_any()
    } else {
        variables.clone()
    };
    if !shared {
        let constructor = frame_global(&scope, pyo3::intern!(py, "dict"))?;
        let globals = if globals.is_truthy()? { globals.clone() } else {
            pyo3::types::PyTuple::empty(py).into_any()
        };
        parent = binding.getattr("_call_with_mapping")?.call1((constructor, globals, parent))?;
    }
    if locals.is_truthy()? {
        if shared { parent = frame_global(&scope, pyo3::intern!(py, "dict"))?.call1((parent,))?; }
        for item in locals.call_method0("items")?.try_iter()? {
            let (key, value) = crate::unpack_two(&item?)?;
            if !value
                .bind(py)
                .is(&frame_global(&scope, pyo3::intern!(py, "missing"))?)
            {
                parent.set_item(key.bind(py), value.bind(py))?;
            }
        }
    }
    crate::python_context::python_new_context(
        py,
        environment,
        &parent,
        name,
        blocks,
        globals,
    )
}

#[pyclass]
struct ContextBlockItems {
    items: Option<Py<PyIterator>>,
    function: Option<Py<PyAny>>,
}

impl ContextBlockItems {
    fn next_item(&self, py: Python<'_>) -> PyResult<Option<(Py<PyAny>, Py<PyAny>)>> {
        let (Some(items), Some(function)) = (&self.items, &self.function) else {
            return Ok(None);
        };
        let mut items = items.bind(py).clone();
        let Some(item) = items.next() else {
            return Ok(None);
        };
        let (key, value) = crate::unpack_two(&item?)?;
        let scope = FrameGlobals::of_function(function.bind(py))?;
        let value = frame_global(&scope, pyo3::intern!(py, "list"))?.call1((value.bind(py),))?;
        Ok(Some((key, value.unbind())))
    }
}

#[pymethods]
impl ContextBlockItems {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }
    fn __next__(&self, py: Python<'_>) -> PyResult<Option<(Py<PyAny>, Py<PyAny>)>> {
        self.next_item(py).map_err(|error| {
            crate::python_errors::escaped_iteration(
                py,
                error,
                crate::python_errors::Boundary::Generator,
            )
        })
    }
    fn send(&self, py: Python<'_>, _value: &Bound<'_, PyAny>) -> PyResult<(Py<PyAny>, Py<PyAny>)> {
        self.__next__(py)?
            .ok_or_else(|| pyo3::exceptions::PyStopIteration::new_err(()))
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.items)?;
        visit.call(&self.function)
    }
    fn __clear__(&mut self) {
        self.items = None;
        self.function = None;
    }
}

#[pyfunction]
pub(crate) fn python_context_derived<'py>(
    py: Python<'py>,
    context: &Bound<'py, PyAny>,
    locals: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let method = context.getattr("derived")?;
    let Some(base) = receiver(
        py,
        &method,
        "_CONTEXT_DERIVED",
        "_CONTEXT_DERIVED_CODE",
        "derived",
    )?
    else {
        return method.call1((locals,));
    };
    let function = method.getattr("__func__")?;
    let scope = FrameGlobals::of_function(&function)?;
    let constructor = frame_global(&scope, pyo3::intern!(py, "new_context"))?;
    let environment = base.getattr("environment")?;
    let name = base.getattr("name")?;
    let blocks = PyDict::new(py);
    let variables = python_context_get_all(py, &base)?;
    let context = new_context(
        py,
        &constructor,
        &environment,
        &name,
        blocks.as_any(),
        &variables,
        true,
        py.None().bind(py),
        locals,
    )?;
    context.setattr("eval_ctx", base.getattr("eval_ctx")?)?;
    let update = context.getattr("blocks")?.getattr("update")?;
    let items = base.getattr("blocks")?.call_method0("items")?.try_iter()?;
    let items = Py::new(
        py,
        ContextBlockItems {
            items: Some(items.unbind()),
            function: Some(function.unbind()),
        },
    )?;
    let generator = crate::python_function::binding(py)?
        .getattr("_context_block_generator")?
        .call1((items,))?;
    update.call1((generator,))?;
    Ok(context)
}
