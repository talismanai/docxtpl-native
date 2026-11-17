//! Compile-time evaluation of the foreign Jinja AST. Parsing and extension
//! callbacks remain separate boundaries; standard evaluation runs in Rust.
//! Rules follow Jinja 3.1.6 nodes.py (BSD-3-Clause; vendor/notices).
use crate::python_function::StandardFunction;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use std::cell::Cell;

thread_local! { static DEPTH: Cell<usize> = const { Cell::new(0) }; }
// A native stack frame is larger than a Python interpreter frame. CPython's
// recursion counter alone does not protect Rust from a cyclic extension AST.
const MAX_DEPTH: usize = 128;
struct DepthGuard;
impl Drop for DepthGuard {
    fn drop(&mut self) {
        DEPTH.with(|depth| depth.set(depth.get() - 1));
    }
}

const NAMES: &[&str] = &[
    "Const",
    "TemplateData",
    "Tuple",
    "List",
    "Dict",
    "Pair",
    "Keyword",
    "CondExpr",
    "Filter",
    "Test",
    "Getitem",
    "Getattr",
    "Slice",
    "Concat",
    "Compare",
    "Mul",
    "Div",
    "FloorDiv",
    "Add",
    "Sub",
    "Mod",
    "Pow",
    "And",
    "Or",
    "Not",
    "Neg",
    "Pos",
    "MarkSafe",
    "MarkSafeIfAutoescape",
    "Name",
    "Call",
    "ContextReference",
    "DerivedContextReference",
    "NSRef",
    "EnvironmentAttribute",
    "ExtensionAttribute",
    "ImportedName",
    "InternalName",
];
type NodeMethod = (&'static str, Py<PyAny>, StandardFunction);
static NODES: pyo3::sync::PyOnceLock<Vec<NodeMethod>> = pyo3::sync::PyOnceLock::new();
static MEMBERSHIP: pyo3::sync::PyOnceLock<Vec<(bool, StandardFunction)>> =
    pyo3::sync::PyOnceLock::new();

pub(crate) fn initialize(py: Python<'_>) -> PyResult<()> {
    NODES.get_or_try_init(py, || {
        let module = py.import("jinja2.nodes")?;
        let mut nodes = Vec::new();
        for name in NAMES {
            let class = module.getattr(*name)?;
            let function = class.getattr("as_const")?;
            if function.is_exact_instance_of::<pyo3::types::PyFunction>() {
                let method = StandardFunction::capture_value(&module, "as_const", &function)?;
                nodes.push((*name, class.unbind(), method));
            }
        }
        Ok::<_, PyErr>(nodes)
    })?;
    MEMBERSHIP.get_or_try_init(py, || {
        let module = py.import("jinja2.nodes")?;
        let functions = module.getattr("_cmpop_to_func")?;
        let mut originals = Vec::new();
        for (name, negate) in [("in", false), ("notin", true)] {
            let function = functions.get_item(name)?;
            if function.is_exact_instance_of::<pyo3::types::PyFunction>() {
                originals.push((negate, StandardFunction::capture_value(
                    &module, "<lambda>", &function,
                )?));
            }
        }
        Ok::<_, PyErr>(originals)
    })?;
    Ok(())
}

fn impossible(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    Err(PyErr::from_value(
        py.import("jinja2.nodes")?.getattr("Impossible")?.call0()?,
    ))
}

fn child<'py>(
    node: &Bound<'py, PyAny>,
    name: &str,
    context: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    evaluate(&node.getattr(name)?, context)
}

fn sequence<'py>(
    node: &Bound<'py, PyAny>,
    field: &str,
    context: &Bound<'py, PyAny>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    node.getattr(field)?
        .try_iter()?
        .map(|item| evaluate(&item?, context))
        .collect()
}

fn filter<'py>(
    node: &Bound<'py, PyAny>,
    context: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = node.py();
    if context.getattr("volatile")?.is_truthy()? || node.getattr("node")?.is_none() {
        return impossible(py);
    }
    let environment = context.getattr("environment")?;
    let registry = if node.getattr("_is_filter")?.is_truthy()? {
        "filters"
    } else {
        "tests"
    };
    let function = environment
        .getattr(registry)?
        .call_method1("get", (node.getattr("name")?,))?;
    // Preserve the public pass-argument protocol, including descriptors.
    let pass = crate::python_call::pass_arg(
        &py.import("jinja2.utils")?
            .getattr("_PassArg")?
            .getattr("from_obj")?,
        &function,
    )?;
    let kinds = py.import("jinja2.utils")?.getattr("_PassArg")?;
    if function.is_none() || pass.is(&kinds.getattr("context")?) {
        return impossible(py);
    }
    if environment.getattr("is_async")?.is_truthy()? {
        let variant = crate::python_format::optional_attribute(&function, "jinja_async_variant")?;
        if variant.is(&true.into_pyobject(py)?)
            || py
                .import("inspect")?
                .call_method1("iscoroutinefunction", (&function,))?
                .is_truthy()?
        {
            return impossible(py);
        }
    }
    // Jinja evaluates trailing arguments before the filtered expression.
    let mut args = sequence(node, "args", context)?;
    let kwargs = PyDict::new(py);
    for item in node.getattr("kwargs")?.try_iter()? {
        let pair = evaluate(&item?, context)?;
        kwargs.set_item(pair.get_item(0)?, pair.get_item(1)?)?;
    }
    let dynamic = node.getattr("dyn_args")?;
    if !dynamic.is_none() {
        for value in evaluate(&dynamic, context)?.try_iter()? {
            args.push(value?);
        }
    }
    let dynamic = node.getattr("dyn_kwargs")?;
    if !dynamic.is_none() {
        kwargs.call_method1("update", (evaluate(&dynamic, context)?,))?;
    }
    args.insert(0, child(node, "node", context)?);
    if pass.is(&kinds.getattr("eval_context")?) {
        args.insert(0, context.clone());
    } else if pass.is(&kinds.getattr("environment")?) {
        args.insert(0, environment);
    }
    crate::python_macro::dispatch(&function, &PyTuple::new(py, args)?, &kwargs)
}

fn evaluate_inner<'py>(
    node: &Bound<'py, PyAny>,
    context: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = node.py();
    let method = node.getattr("as_const")?;
    let Some((name, _, guard)) = NODES.get(py).and_then(|nodes| {
        nodes
            .iter()
            .find(|(_, class, _)| node.get_type().is(class.bind(py)))
    }) else {
        return method.call1((context,));
    };
    let function = crate::python_format::optional_attribute(&method, "__func__")?;
    if !guard.matches(&function)? {
        return method.call1((context,));
    }
    match *name {
        "Const" => node.getattr("value"),
        "Name"
        | "Call"
        | "ContextReference"
        | "DerivedContextReference"
        | "NSRef"
        | "EnvironmentAttribute"
        | "ExtensionAttribute"
        | "ImportedName"
        | "InternalName" => impossible(py),
        "TemplateData" => {
            if context.getattr("volatile")?.is_truthy()? {
                return impossible(py);
            }
            let data = node.getattr("data")?;
            if context.getattr("autoescape")?.is_truthy()? {
                py.import("jinja2.nodes")?.getattr("Markup")?.call1((data,))
            } else {
                Ok(data)
            }
        }
        "Tuple" => Ok(PyTuple::new(py, sequence(node, "items", context)?)?.into_any()),
        "List" => Ok(PyList::new(py, sequence(node, "items", context)?)?.into_any()),
        "Dict" => {
            let result = PyDict::new(py);
            // Insert while traversing: hashing can mutate a later expression.
            for item in node.getattr("items")?.try_iter()? {
                let pair = evaluate(&item?, context)?;
                result.set_item(pair.get_item(0)?, pair.get_item(1)?)?;
            }
            Ok(result.into_any())
        }
        "Pair" | "Keyword" => {
            let key = if *name == "Pair" {
                child(node, "key", context)?
            } else {
                node.getattr("key")?
            };
            Ok(PyTuple::new(py, [key, child(node, "value", context)?])?.into_any())
        }
        "And" | "Or" => {
            let left = child(node, "left", context)?;
            if left.is_truthy()? == (*name == "And") {
                child(node, "right", context)
            } else {
                Ok(left)
            }
        }
        "CondExpr" => {
            let field = if child(node, "test", context)?.is_truthy()? {
                "expr1"
            } else {
                "expr2"
            };
            if node.getattr(field)?.is_none() {
                impossible(py)
            } else {
                child(node, field, context)
            }
        }
        "Filter" | "Test" => filter(node, context),
        "Getitem" | "Getattr" => {
            if !node.getattr("ctx")?.eq("load")? {
                return impossible(py);
            }
            let method = context
                .getattr("environment")?
                .getattr(if *name == "Getattr" {
                    "getattr"
                } else {
                    "getitem"
                })?;
            let value = child(node, "node", context)?;
            let key = if *name == "Getitem" {
                child(node, "arg", context)?
            } else {
                node.getattr("attr")?
            };
            crate::python_lookup::dispatch_lookup(
                &method,
                &value,
                &key,
                *name == "Getattr",
                &crate::python_function::binding(py)?,
            )
        }
        "Slice" => {
            let mut args = Vec::new();
            for name in ["start", "stop", "step"] {
                let value = node.getattr(name)?;
                args.push(if value.is_none() {
                    value
                } else {
                    evaluate(&value, context)?
                });
            }
            py.get_type::<pyo3::types::PySlice>()
                .call1(PyTuple::new(py, args)?)
        }
        "Concat" => {
            let result = PyList::empty(py);
            for value in node.getattr("nodes")?.try_iter()? {
                result.append(evaluate(&value?, context)?.str()?)?;
            }
            pyo3::types::PyString::new(py, "").call_method1("join", (result,))
        }
        "Compare" => {
            let mut value = child(node, "expr", context)?;
            let mut result = value.clone();
            for operand in node.getattr("ops")?.try_iter()? {
                let operand = operand?;
                let new = child(&operand, "expr", context)?;
                let function = py.import("jinja2.nodes")?
                    .getattr("_cmpop_to_func")?
                    .get_item(operand.getattr("op")?)?;
                let mut membership = None;
                if let Some(originals) = MEMBERSHIP.get(py) {
                    for (negate, original) in originals {
                        if original.matches(&function)? {
                            membership = Some(*negate);
                            break;
                        }
                    }
                }
                if let Some(negate) = membership {
                    let contains =
                        crate::python_operators::python_operator(py, "contains", &value, &new)?;
                    result = if negate {
                        (!contains.is_truthy()?)
                            .into_pyobject(py)?
                            .to_owned()
                            .into_any()
                    } else {
                        contains
                    };
                } else {
                    result = function.call1((&value, &new))?;
                }
                if !result.is_truthy()? {
                    return Ok(false.into_pyobject(py)?.to_owned().into_any());
                }
                value = new;
            }
            Ok(result)
        }
        "MarkSafe" | "MarkSafeIfAutoescape" => {
            if *name == "MarkSafeIfAutoescape" && context.getattr("volatile")?.is_truthy()? {
                return impossible(py);
            }
            let value = child(node, "expr", context)?;
            if *name == "MarkSafe" || context.getattr("autoescape")?.is_truthy()? {
                py.import("jinja2.nodes")?
                    .getattr("Markup")?
                    .call1((value,))
            } else {
                Ok(value)
            }
        }
        _ => {
            let unary = ["Not", "Neg", "Pos"].contains(name);
            let operator = node.getattr("operator")?;
            let environment = context.getattr("environment")?;
            if environment.getattr("sandboxed")?.is_truthy()?
                && environment
                    .getattr(if unary {
                        "intercepted_unops"
                    } else {
                        "intercepted_binops"
                    })?
                    .contains(&operator)?
            {
                return impossible(py);
            }
            let functions = py.import("jinja2.nodes")?.getattr(if unary {
                "_uaop_to_func"
            } else {
                "_binop_to_func"
            })?;
            let function = functions.get_item(operator)?;
            if unary {
                function.call1((child(node, "node", context)?,))
            } else {
                function.call1((
                    child(node, "left", context)?,
                    child(node, "right", context)?,
                ))
            }
        }
    }
}

#[pyfunction]
pub(crate) fn constant_value<'py>(
    node: &Bound<'py, PyAny>,
    context: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    evaluate(node, context)
}

fn evaluate<'py>(
    node: &Bound<'py, PyAny>,
    context: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let entered = DEPTH.with(|depth| {
        if depth.get() >= MAX_DEPTH {
            false
        } else {
            depth.set(depth.get() + 1);
            true
        }
    });
    if !entered {
        return Err(pyo3::exceptions::PyRecursionError::new_err(
            "maximum recursion depth exceeded while evaluating a template constant",
        ));
    }
    let _depth = DepthGuard;
    // The outer compiler handles ordinary failures by retrying at runtime.
    // Guard recursive extension ASTs just as CPython guards Python calls.
    if unsafe {
        pyo3::ffi::Py_EnterRecursiveCall(c" while evaluating a template constant".as_ptr())
    } != 0
    {
        return Err(PyErr::fetch(node.py()));
    }
    let result = evaluate_inner(node, context);
    unsafe {
        pyo3::ffi::Py_LeaveRecursiveCall();
    }
    result
}
