//! Standard foreign-context builders; object identity remains owned by CPython.
use crate::pyglobals::{FrameGlobals, frame_global};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyType};

pub(crate) enum Kind {
    Context,
    EvalContext,
    Macro,
    TemplateReference,
}

pub(crate) fn standard_instance<'py>(
    py: Python<'py>,
    class: &Bound<'py, PyAny>,
    kind: Kind,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let binding = crate::python_function::binding(py)?;
    let (class_key, function_key, code_key, module) = match kind {
        Kind::Macro => ("_MACRO_TYPE", "_MACRO_INIT", "_MACRO_INIT_CODE", "_RUNTIME_MODULE"),
        Kind::TemplateReference => ("_TEMPLATE_REFERENCE_TYPE", "_TEMPLATE_REFERENCE_INIT", "_TEMPLATE_REFERENCE_INIT_CODE", "_RUNTIME_MODULE"),
        Kind::Context => (
            "_Context",
            "_CONTEXT_INIT",
            "_CONTEXT_INIT_CODE",
            "_RUNTIME_MODULE",
        ),
        Kind::EvalContext => (
            "_EVAL_CONTEXT_TYPE",
            "_EVAL_CONTEXT_INIT",
            "_EVAL_CONTEXT_INIT_CODE",
            "_NODES_MODULE",
        ),
    };
    if !class.is(&binding.getattr(class_key)?)
        || !class.get_type().is(&py.get_type::<PyType>())
        || !class
            .getattr("__new__")?
            .is(&binding.getattr("_OBJECT_NEW")?)
        || !crate::python_function::is_standard_function(
            &class.getattr("__init__")?,
            &binding.getattr(function_key)?,
            &binding.getattr(code_key)?,
            binding.getattr(module)?.cast()?,
            "__init__",
        )?
    {
        return Ok(None);
    }
    // The exact class uses object.__new__. CPython's allocator provides its
    // ordinary instance dict, weakrefs and GC layout; no foreign layout is copied.
    let object =
        unsafe { pyo3::ffi::PyType_GenericAlloc(class.cast::<PyType>()?.as_type_ptr(), 0) };
    Ok(Some(unsafe { Bound::from_owned_ptr_or_err(py, object)? }))
}

pub(crate) fn template_reference<'py>(
    py: Python<'py>, class: &Bound<'py, PyAny>, context: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let initializer = class.getattr("__init__")?;
    // A replacement installed before our snapshot can retain the original
    // filename/name. The standard initializer has just this field store.
    let standard_fields = initializer.getattr("__code__")
        .and_then(|code| code.getattr("co_names"))
        .and_then(|names| names.extract::<Vec<String>>())
        .is_ok_and(|names| names == ["_TemplateReference__context"]);
    if standard_fields {
        if let Some(reference) = standard_instance(py, class, Kind::TemplateReference)? {
            reference.setattr("_TemplateReference__context", context)?;
            return Ok(reference);
        }
    }
    class.call1((context,))
}

pub(crate) fn eval_context<'py>(
    py: Python<'py>,
    class: &Bound<'py, PyAny>,
    environment: &Bound<'py, PyAny>,
    name: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let Some(context) = standard_instance(py, class, Kind::EvalContext)? else {
        // Keep the original call shape for user constructors: Context supplies
        // the name explicitly, while filters/finalizers omit the optional name.
        return match name {
            Some(name) => class.call1((environment, name)),
            None => class.call1((environment,)),
        };
    };
    let initializer = crate::python_function::binding(py)?
        .getattr("_EVAL_CONTEXT_INIT")?;
    // Argument defaults belong to the function object and can change without
    // replacing its code. Resolve them before any initializer side effects.
    let name = match name {
        Some(name) => name.clone(),
        None => {
            let defaults = initializer.getattr("__defaults__")?;
            if defaults.is_none() || defaults.len()? == 0 {
                return Err(pyo3::exceptions::PyTypeError::new_err(format!(
                    "{}() missing 1 required positional argument: 'template_name'",
                    initializer.getattr("__qualname__")?.str()?
                )));
            }
            defaults.get_item(defaults.len()? - 1)?
        }
    };
    context.setattr(pyo3::intern!(py, "environment"), environment)?;
    let scope = FrameGlobals::of_function(&initializer)?;
    // The reference checks callability, then reads the attribute again.
    let callable = frame_global(&scope, pyo3::intern!(py, "callable"))?
        .call1((environment.getattr("autoescape")?,))?
        .is_truthy()?;
    let setting = environment.getattr("autoescape")?;
    let autoescape = if callable {
        setting.call1((name,))?
    } else {
        setting
    };
    context.setattr(pyo3::intern!(py, "autoescape"), autoescape)?;
    context.setattr(pyo3::intern!(py, "volatile"), false)?;
    Ok(context)
}

#[pyfunction]
pub(crate) fn python_eval_context<'py>(
    py: Python<'py>,
    environment: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let class = py.import("jinja2.nodes")?.getattr("EvalContext")?;
    eval_context(py, &class, environment, None)
}

#[pyfunction]
pub(crate) fn python_new_context<'py>(
    py: Python<'py>,
    environment: &Bound<'py, PyAny>,
    parent: &Bound<'py, PyAny>,
    name: &Bound<'py, PyAny>,
    blocks: &Bound<'py, PyAny>,
    globals: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let class = environment.getattr("context_class")?;
    let Some(context) = standard_instance(py, &class, Kind::Context)? else {
        let kwargs = PyDict::new(py);
        kwargs.set_item("globals", globals)?;
        return class.call((environment, parent, name, blocks), Some(&kwargs));
    };
    context.setattr(pyo3::intern!(py, "parent"), parent)?;
    context.setattr(pyo3::intern!(py, "vars"), PyDict::new(py))?;
    context.setattr(pyo3::intern!(py, "environment"), environment)?;
    // Context.__init__ resolves its own module global, independently of the
    // nodes.EvalContext alias used by direct filter/finalizer injection.
    let initializer = crate::python_function::binding(py)?
        .getattr("_CONTEXT_INIT")?;
    let scope = FrameGlobals::of_function(&initializer)?;
    let constructor = frame_global(&scope, pyo3::intern!(py, "EvalContext"))?;
    let stored_environment = context.getattr(pyo3::intern!(py, "environment"))?;
    context.setattr(
        pyo3::intern!(py, "eval_ctx"),
        eval_context(py, &constructor, &stored_environment, Some(name))?,
    )?;
    context.setattr(
        pyo3::intern!(py, "exported_vars"),
        frame_global(&scope, pyo3::intern!(py, "set"))?.call0()?,
    )?;
    context.setattr(pyo3::intern!(py, "name"), name)?;
    let constructor = frame_global(&scope, pyo3::intern!(py, "set"))?;
    let globals_keys = if globals.is_none() {
        constructor.call0()?
    } else {
        constructor.call1((globals,))?
    };
    context.setattr(pyo3::intern!(py, "globals_keys"), globals_keys)?;
    let new_blocks = PyDict::new(py);
    for item in blocks
        .call_method0(pyo3::intern!(py, "items"))?
        .try_iter()?
    {
        let (key, value) = crate::unpack_two(&item?)?;
        new_blocks.set_item(key.bind(py), PyList::new(py, [value.bind(py)])?)?;
    }
    context.setattr(pyo3::intern!(py, "blocks"), new_blocks)?;
    Ok(context)
}

#[pyfunction]
pub(crate) fn python_context<'py>(
    py: Python<'py>,
    config: &Bound<'py, PyAny>,
    visible: &Bound<'py, PyAny>,
    autoescape: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let environment = config.getattr("environment")?;
    let parent = py
        .get_type::<PyDict>()
        .call1((environment.getattr("globals")?,))?;
    parent.call_method1("update", (visible,))?;
    let context = python_new_context(
        py,
        &environment,
        &parent,
        py.None().bind(py),
        &PyDict::new(py),
        &environment.getattr("globals")?,
    )?;
    context
        .getattr("eval_ctx")?
        .setattr("autoescape", autoescape)?;
    Ok(context)
}

fn standard_eval_method(py: Python<'_>, method: &Bound<'_, PyAny>, name: &str) -> PyResult<bool> {
    let binding = crate::python_function::binding(py)?;
    let prefix = format!("_EVAL_CONTEXT_{}", name.to_uppercase());
    Ok(method.get_type().is(&binding.getattr("_METHOD_TYPE")?) &&
        crate::python_function::is_standard_function(&method.getattr("__func__")?,
            &binding.getattr(&prefix)?, &binding.getattr(format!("{prefix}_CODE"))?,
            binding.getattr("_NODES_MODULE")?.cast()?, name)?)
}

pub(crate) fn eval_context_save<'py>(py: Python<'py>, context: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let method = context.getattr("save")?;
    if standard_eval_method(py, &method, "save")? {
        context.getattr("__dict__")?.call_method0("copy")
    } else { method.call0() }
}

pub(crate) fn eval_context_revert(py: Python<'_>, context: &Bound<'_, PyAny>, saved: &Bound<'_, PyAny>) -> PyResult<()> {
    let method = context.getattr("revert")?;
    if standard_eval_method(py, &method, "revert")? {
        context.getattr("__dict__")?.call_method0("clear")?;
        context.getattr("__dict__")?.call_method1("update", (saved,))?;
    } else { method.call1((saved,))?; }
    Ok(())
}
