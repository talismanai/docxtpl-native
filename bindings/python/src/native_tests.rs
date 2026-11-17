//! Builtin predicate algorithms. CPython is used only for object/type identity.
//! User supplied registry callbacks continue through the binding.
use crate::pyglobals::{FrameGlobals, frame_global, handle_exception};
use crate::python_bridge::Bridge;
use minijinja::value::{Rest, Value};
use minijinja::{Environment, Error, State};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFunction, PyInt, PyList, PyTuple};
use std::sync::Arc;

const NAMES: &[&str] = &[
    "defined",
    "undefined",
    "none",
    "boolean",
    "false",
    "true",
    "integer",
    "float",
    "string",
    "sameas",
    "odd",
    "even",
    "divisibleby",
    "lower",
    "upper",
    "mapping",
    "number",
    "sequence",
    "iterable",
    "escaped",
    "in",
];

static FUNCTIONS: pyo3::sync::PyOnceLock<Vec<(String, crate::python_function::StandardFunction)>> =
    pyo3::sync::PyOnceLock::new();

pub(crate) fn initialize(py: Python<'_>) -> PyResult<()> {
    FUNCTIONS.get_or_try_init(py, || {
        let module = py.import("jinja2.tests")?;
        let mut functions = Vec::new();
        for name in NAMES.iter().copied().chain(["filter", "test"]) {
            let function = format!("test_{name}");
            if module
                .getattr(function.as_str())?
                .is_exact_instance_of::<PyFunction>()
            {
                functions.push((
                    name.to_owned(),
                    crate::python_function::StandardFunction::capture(&module, &function)?,
                ));
            }
        }
        Ok::<_, PyErr>(functions)
    })?;
    Ok(())
}

pub(crate) fn try_call<'py>(
    callback: &Bound<'py, PyAny>,
    args: &Bound<'py, PyTuple>,
    kwargs: &Bound<'py, PyDict>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    if let Some(functions) = FUNCTIONS.get(callback.py()) {
        for (name, function) in functions {
            if function.matches(callback)? {
                let values = crate::python_function::bind_fixed(callback, args, kwargs)?;
                return evaluate(name, callback, &values).map(Some);
            }
        }
    }
    Ok(None)
}

// Match the pinned Python signatures, including duplicate/unknown keyword errors.
fn arguments(
    bridge: &Arc<Bridge>,
    function: &Bound<'_, PyAny>,
    args: &[Value],
    kwargs: &Bound<'_, PyDict>,
) -> PyResult<Vec<Value>> {
    let py = function.py();
    let positional = PyTuple::new(
        py,
        args.iter()
            .filter(|arg| !arg.is_kwargs())
            .map(|arg| bridge.python(py, arg))
            .collect::<PyResult<Vec<_>>>()?,
    )?;
    Ok(
        crate::python_function::bind_fixed(function, &positional, kwargs)?
            .iter()
            .map(|value| bridge.opaque_value(value))
            .collect(),
    )
}

fn keywords<'py>(
    bridge: &Arc<Bridge>,
    function: &Bound<'py, PyAny>,
    args: &[Value],
) -> PyResult<Bound<'py, PyDict>> {
    let py = function.py();
    let sources = PyList::empty(py);
    for arg in args.iter().filter(|arg| arg.is_kwargs()) {
        if let Some(originals) = arg.keyword_sources() {
            for source in originals {
                sources.append(bridge.python(py, source)?)?;
            }
        } else {
            sources.append(bridge.python(py, arg)?)?;
        }
    }
    crate::python_keywords::merge(function, &sources)
}

enum Prepared {
    Native(Vec<Value>),
    Override(Value),
}

pub fn install(
    env: &mut Environment<'static>,
    bridge: &Arc<Bridge>,
    name: &str,
    callback: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    if !NAMES.contains(&name) {
        return Ok(false);
    }
    let py = callback.py();
    let defaults = py.import("jinja2.defaults")?.getattr("DEFAULT_TESTS")?;
    if !defaults
        .cast::<PyDict>()?
        .get_item(name)?
        .is_some_and(|original| original.is(callback))
    {
        return Ok(false);
    }
    if !callback.is_exact_instance_of::<PyFunction>() {
        return Ok(false);
    }
    let Some((_, original)) = FUNCTIONS
        .get(py)
        .and_then(|functions| functions.iter().find(|(registered, _)| registered == name))
    else {
        return Ok(false);
    };
    if !original.matches(callback)? {
        return Ok(false);
    }
    let module = py.import("jinja2.tests")?;
    let code = callback.getattr("__code__")?;
    let function_name = format!("test_{name}");
    if !crate::python_function::is_standard_function(
        callback,
        callback,
        &code,
        &module,
        &function_name,
    )? {
        return Ok(false);
    }
    let callback = callback.clone().unbind();
    let code = code.unbind();
    let module = module.unbind();
    let name = name.to_owned();
    let local = bridge.clone();
    env.add_test(
        name.clone(),
        move |_state: &State, args: Rest<Value>| -> Result<Value, Error> {
            let prepared = Python::attach(|py| -> PyResult<Prepared> {
                let callback = callback.bind(py);
                let kwargs = keywords(&local, callback, &args)?;
                // A mapping callback may replace the function's code. The
                // call frame is selected after unpacking, before key matching.
                if !crate::python_function::is_standard_function(
                    callback,
                    callback,
                    code.bind(py),
                    module.bind(py),
                    &function_name,
                )? {
                    let positional = args
                        .iter()
                        .filter(|arg| !arg.is_kwargs())
                        .map(|arg| local.python(py, arg))
                        .collect::<PyResult<Vec<_>>>()?;
                    return Ok(Prepared::Override(local.value(
                        &callback.call(PyTuple::new(py, positional)?, Some(&kwargs))?,
                    )?));
                }
                Ok(Prepared::Native(arguments(
                    &local, callback, &args, &kwargs,
                )?))
            })
            .map_err(|error| local.remember(error))?;
            let args = match prepared {
                Prepared::Native(args) => args,
                Prepared::Override(value) => return Ok(value),
            };
            Python::attach(|py| -> PyResult<Value> {
                let values = args
                    .iter()
                    .map(|arg| local.python(py, arg).map(|v| v.into_bound(py)))
                    .collect::<PyResult<Vec<_>>>()?;
                let result = evaluate(&name, callback.bind(py), &values)?;
                if result.is_exact_instance_of::<PyBool>() {
                    Ok(Value::from(result.extract::<bool>()?))
                } else {
                    Ok(local.opaque_value(&result))
                }
            })
            .map_err(|error| local.remember(error))
        },
    );
    Ok(true)
}

fn evaluate<'py>(
    name: &str,
    callback: &Bound<'py, PyAny>,
    args: &[Bound<'py, PyAny>],
) -> PyResult<Bound<'py, PyAny>> {
    let py = callback.py();
    let value = &args[0];
    let result = match name {
        "filter" | "test" => args[0]
            .getattr(if name == "filter" { "filters" } else { "tests" })?
            .contains(&args[1])?,
        "none" => value.is_none(),
        "boolean" => value.is_instance_of::<PyBool>(),
        "false" => value.is_instance_of::<PyBool>() && !value.extract::<bool>()?,
        "true" => value.is_instance_of::<PyBool>() && value.extract::<bool>()?,
        "defined" | "undefined" | "integer" | "float" | "string" | "mapping" | "number" => {
            let scope = FrameGlobals::of_function(callback)?;
            // LOAD_GLOBAL resolves the callable before its class
            // argument; preserve live aliases and original returns.
            let check = frame_global(&scope, pyo3::intern!(py, "isinstance"))?;
            let class_name = match name {
                "defined" | "undefined" => pyo3::intern!(py, "Undefined"),
                "integer" => pyo3::intern!(py, "int"),
                "float" => pyo3::intern!(py, "float"),
                "number" => pyo3::intern!(py, "Number"),
                "mapping" => pyo3::intern!(py, "abc"),
                _ => pyo3::intern!(py, "str"),
            };
            let mut class = frame_global(&scope, class_name)?;
            if name == "mapping" {
                class = class.getattr(pyo3::intern!(py, "Mapping"))?;
            }
            let checked = check.call1((value, class))?;
            if name == "defined" {
                !checked.is_truthy()?
            } else if name == "integer" && checked.is_truthy()? {
                !value.is_instance_of::<PyBool>()
            } else {
                return Ok(checked);
            }
        }
        "odd" | "even" | "divisibleby" => {
            let divisor = if name == "divisibleby" {
                args[1].clone()
            } else {
                PyInt::new(py, 2).into_any()
            };
            let remainder = crate::python_operators::python_operator(py, "rem", value, &divisor)?;
            let expected = PyInt::new(py, if name == "odd" { 1 } else { 0 }).into_any();
            return crate::python_operators::python_operator(py, "==", &remainder, &expected);
        }
        "lower" | "upper" => {
            let scope = FrameGlobals::of_function(callback)?;
            let converted = frame_global(&scope, pyo3::intern!(py, "str"))?.call1((value,))?;
            let method = if name == "lower" {
                pyo3::intern!(py, "islower")
            } else {
                pyo3::intern!(py, "isupper")
            };
            return converted.call_method0(method);
        }
        "escaped" => {
            let scope = FrameGlobals::of_function(callback)?;
            return frame_global(&scope, pyo3::intern!(py, "hasattr"))?
                .call1((value, pyo3::intern!(py, "__html__")));
        }
        "sequence" | "iterable" => {
            let scope = FrameGlobals::of_function(callback)?;
            let attempt = (|| -> PyResult<()> {
                if name == "sequence" {
                    frame_global(&scope, pyo3::intern!(py, "len"))?.call1((value,))?;
                    value.getattr(pyo3::intern!(py, "__getitem__"))?;
                } else {
                    frame_global(&scope, pyo3::intern!(py, "iter"))?.call1((value,))?;
                }
                Ok(())
            })();
            match attempt {
                Ok(()) => true,
                Err(error) => handle_exception(
                    &scope,
                    error,
                    if name == "sequence" {
                        &["Exception"]
                    } else {
                        &["TypeError"]
                    },
                    || Ok(false),
                )?,
            }
        }
        "in" => {
            return crate::python_operators::python_operator(py, "contains", value, &args[1]);
        }
        "sameas" => value.is(&args[1]),
        _ => unreachable!(),
    };
    Ok(result.into_pyobject(py)?.to_owned().into_any())
}
