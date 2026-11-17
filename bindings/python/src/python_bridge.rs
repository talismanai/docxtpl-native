//! Owned CPython references for the native runtime. No raw core pointers or
//! unsafe Send wrappers: each !Send document core stays inside its detach call.
//! This is a tested bridge prototype, not a claim of complete Jinja2 semantics.
use minijinja::value::{Enumerator, Object, ObjectRepr, Rest, Value, ValueKind};
use minijinja::{AutoEscape, Environment, Error, ErrorKind, RuntimeContainer, RuntimeUnpack, State};
use pyo3::exceptions::{PyAttributeError, PyRuntimeError, PyStopIteration, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyComplex, PyDict, PyEllipsis, PyFloat, PyFrozenSet, PyFunction, PyInt, PyList, PyModule, PyNotImplemented, PyRange, PySet, PyString, PyTuple, PyType};
use pyo3::IntoPyObjectExt;
use std::borrow::Cow;
use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::fmt;
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

static IMAGE_TYPE: pyo3::sync::PyOnceLock<Py<PyType>> = pyo3::sync::PyOnceLock::new();

/// Keep the package's own image class, even if the public name is replaced later.
pub(crate) fn capture_image_type(py: Python<'_>) -> PyResult<()> {
    if let Ok(package) = py.import("docxtpl_native") {
        if let Some(image) = package.dict().get_item("_OriginalInlineImage")? {
            if let Ok(image) = image.cast_into::<PyType>() {
                let _ = IMAGE_TYPE.set(py, image.unbind());
            }
        }
    }
    Ok(())
}

pub(crate) type LocalErrorSlot = Rc<RefCell<Option<PyErr>>>;
// A thread-local stack isolates nested renders and different bridges without
// holding a lock while Python callbacks execute. Async fibers install their
// own slot only while they execute; other entry points keep their own slot.
thread_local! {
    static ERROR_SCOPES: RefCell<Vec<(usize, LocalErrorSlot)>> = const { RefCell::new(Vec::new()) };
}

// Code on a render fiber may run on another thread after a suspension (see
// `fiber_local`): the scopes are only accessed through functions that are
// never inlined, so their address is computed on the running thread.
#[inline(never)]
fn push_error_scope(bridge: usize, slot: LocalErrorSlot) {
    ERROR_SCOPES.with(|scopes| scopes.borrow_mut().push((bridge, slot)));
}

#[inline(never)]
fn pop_error_scope() -> Option<(usize, LocalErrorSlot)> {
    ERROR_SCOPES.with(|scopes| scopes.borrow_mut().pop())
}

#[inline(never)]
fn find_error_scope(bridge: usize) -> Option<LocalErrorSlot> {
    ERROR_SCOPES.with(|scopes| scopes.borrow().iter().rev()
        .find(|(candidate, _)| *candidate == bridge).map(|(_, slot)| slot.clone()))
}

/// The number of error scopes open on this thread.
#[inline(never)]
pub(crate) fn error_scope_depth() -> usize {
    ERROR_SCOPES.with(|scopes| scopes.borrow().len())
}

struct ErrorScope { bridge: usize }
impl Drop for ErrorScope {
    fn drop(&mut self) {
        let popped = pop_error_scope();
        debug_assert!(popped.as_ref().is_some_and(|(bridge, _)| *bridge == self.bridge));
        drop(popped);
    }
}

fn resolve_nonfinite(py: Python<'_>, number: f64) -> PyResult<Bound<'_, PyAny>> {
    let name = if number.is_nan() { "nan" } else { "inf" };
    let builtins = py.import("builtins")?;
    let value = match builtins.dict().get_item(name)? {
        Some(value) => value,
        None => {
            let kwargs = PyDict::new(py);
            kwargs.set_item("name", name)?;
            let exception = py.get_type::<pyo3::exceptions::PyNameError>()
                .call((format!("name '{name}' is not defined"),), Some(&kwargs))?;
            return Err(PyErr::from_value(exception));
        }
    };
    if number.is_infinite() && number.is_sign_negative() {
        unsafe { Bound::from_owned_ptr_or_err(py, pyo3::ffi::PyNumber_Negative(value.as_ptr())) }
    } else { Ok(value) }
}

// Only literal forms that can be rebuilt without sharing mutable objects are
// folded. Other values retain the existing runtime path.
fn constant_argument(value: &Bound<'_, PyAny>, seen: &mut HashSet<usize>) -> Option<minijinja::ConstantArgument> {
    use minijinja::ConstantArgument as C;
    if value.is_none() { return Some(C::Value(Value::from(()))); }
    if value.is_exact_instance_of::<PyBool>() { return value.extract::<bool>().ok().map(|x| C::Value(Value::from(x))); }
    if value.is_exact_instance_of::<PyInt>() { return value.extract::<i128>().ok().map(|x| C::Value(Value::from(x))); }
    if value.is_exact_instance_of::<PyFloat>() {
        return value.extract::<f64>().ok().filter(|x| x.is_finite()).map(|x| C::Value(Value::from(x)));
    }
    if value.is_exact_instance_of::<PyString>() { return value.extract::<String>().ok().map(|x| C::Value(Value::from(x))); }
    if (value.is_exact_instance_of::<PyList>() || value.is_exact_instance_of::<PyTuple>()
        || value.is_exact_instance_of::<PyDict>()) && seen.len() < 128 {
        let key = value.as_ptr() as usize;
        if !seen.insert(key) { return None; }
        let result = if let Ok(dict) = value.cast::<PyDict>() {
            dict.iter().map(|(key, value)| Some((constant_argument(&key, seen)?, constant_argument(&value, seen)?)))
                .collect::<Option<Vec<_>>>().map(C::Map)
        } else if let Ok(tuple) = value.cast::<PyTuple>() {
            tuple.iter().map(|item| constant_argument(&item, seen)).collect::<Option<Vec<_>>>().map(C::Tuple)
        } else {
            value.cast::<PyList>().ok()?.iter().map(|item| constant_argument(&item, seen)).collect::<Option<Vec<_>>>().map(C::List)
        };
        seen.remove(&key);
        return result;
    }
    None
}

fn safe_constant_repr(value: &Bound<'_, PyAny>, seen: &mut HashSet<usize>) -> PyResult<bool> {
    if value.is_none() || value.is_exact_instance_of::<PyBool>() || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyFloat>() || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyComplex>() || value.is_exact_instance_of::<PyRange>()
        || value.is_exact_instance_of::<PyEllipsis>() || value.is_exact_instance_of::<PyNotImplemented>() { return Ok(true); }
    if (value.is_exact_instance_of::<PyList>() || value.is_exact_instance_of::<PyTuple>()
        || value.is_exact_instance_of::<PySet>() || value.is_exact_instance_of::<PyFrozenSet>()
        || value.is_exact_instance_of::<PyDict>()) && seen.len() < 128 {
        let key = value.as_ptr() as usize;
        if !seen.insert(key) { return Err(pyo3::exceptions::PyRecursionError::new_err("maximum recursion depth exceeded")); }
        let safe = if let Ok(dict) = value.cast::<PyDict>() {
            let mut safe = true;
            for (key, item) in dict.iter() {
                if !safe_constant_repr(&key, seen)? || !safe_constant_repr(&item, seen)? { safe = false; break; }
            }
            safe
        } else {
            let mut safe = true;
            for item in value.try_iter()? {
                if !safe_constant_repr(&item?, seen)? { safe = false; break; }
            }
            safe
        };
        seen.remove(&key);
        return Ok(safe);
    }
    Ok(value.py().import("markupsafe").ok().and_then(|m| m.getattr("Markup").ok())
        .is_some_and(|markup| value.get_type().is(&markup)))
}

// Capture literal order without rebuilding hash tables. Rehashing a set while
// taking its snapshot changes the repr that the reference compiler emits.
enum ConstantSnapshot {
    Atom(Py<PyAny>),
    List(Vec<Self>),
    Tuple(Vec<Self>),
    Map(Vec<(Self, Self)>),
    Set { values: Vec<Self>, frozen: bool, compiled: Option<Py<PyAny>> },
}

impl ConstantSnapshot {
    fn capture(value: &Bound<'_, PyAny>, pool: &Bound<'_, PyDict>) -> PyResult<Self> {
        let py = value.py();
        if value.is_exact_instance_of::<PyDict>() {
            return value.cast::<PyDict>()?.iter().map(|(key, value)| Ok((Self::capture(&key, pool)?, Self::capture(&value, pool)?)))
                .collect::<PyResult<Vec<_>>>().map(Self::Map);
        }
        let set = value.is_exact_instance_of::<PySet>() || value.is_exact_instance_of::<PyFrozenSet>();
        if set || value.is_exact_instance_of::<PyList>() || value.is_exact_instance_of::<PyTuple>() {
            let values = value.try_iter()?.map(|item| Self::capture(&item?, pool)).collect::<PyResult<Vec<_>>>()?;
            if value.is_exact_instance_of::<PyList>() { return Ok(Self::List(values)); }
            if value.is_exact_instance_of::<PyTuple>() { return Ok(Self::Tuple(values)); }
            // CPython folds >=3 constant set elements into a frozenset, then
            // SET_UPDATE copies that table into the runtime set. The C API owns
            // hashing and table sizing; no hash-table implementation is copied.
            let compiled = if values.len() >= 3 && values.iter().all(|item| item.is_literal(py)) {
                let items = values.iter().map(|item| item.materialize(py)).collect::<PyResult<Vec<_>>>()?;
                let keys = items.iter().map(Self::literal_key).collect::<PyResult<Vec<_>>>()?;
                let key = PyFrozenSet::new(py, keys)?;
                if let Some(compiled) = pool.get_item(&key)? {
                    return Ok(Self::Set { values, frozen: value.is_exact_instance_of::<PyFrozenSet>(), compiled: Some(compiled.unbind()) });
                }
                let folded = PyFrozenSet::new(py, items)?;
                // CPython's constant-pool merge rebuilds the folded table from
                // its iteration order before SET_UPDATE consumes it.
                let merged = PyFrozenSet::new(py, folded.iter())?;
                // Code-object construction interns identifier-like strings;
                // replacement of a direct element rebuilds the frozen table.
                let mut items = merged.iter().collect::<Vec<_>>();
                let mut changed = false;
                for item in items.iter_mut().rev() { changed |= Self::intern_literal(item)?; }
                let merged = if changed { PyFrozenSet::new(py, items)? } else { merged };
                pool.set_item(key, &merged)?;
                Some(merged.into_any().unbind())
            } else { None };
            return Ok(Self::Set { values, frozen: value.is_exact_instance_of::<PyFrozenSet>(), compiled });
        }
        if value.is_exact_instance_of::<PyString>() {
            if let Some(text) = value.cast::<PyString>().ok().and_then(|text| text.to_str().ok()) {
                return Ok(Self::Atom(PyString::new(py, text).into_any().unbind()));
            }
        }
        Ok(Self::Atom(value.clone().unbind()))
    }

    fn literal_key<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        let key = if let Ok(tuple) = value.cast::<PyTuple>() {
            PyTuple::new(py, tuple.iter().map(|item| Self::literal_key(&item)).collect::<PyResult<Vec<_>>>()?)?.into_any()
        } else if value.is_exact_instance_of::<PyFloat>() {
            // Python numeric equality merges bool/int/float and signed zero;
            // a compiled literal must retain its exact type and sign.
            value.extract::<f64>()?.to_bits().into_pyobject(py)?.into_any()
        } else { value.clone() };
        Ok((value.get_type(), key).into_pyobject(py)?.into_any())
    }

    fn intern_literal(value: &mut Bound<'_, PyAny>) -> PyResult<bool> {
        let py = value.py();
        if let Ok(text) = value.cast::<PyString>() {
            if text.to_str().is_ok_and(|text| text.bytes().all(|ch| ch.is_ascii_alphanumeric() || ch == b'_')) {
                let mut pointer = value.clone().unbind().into_ptr();
                // CPython owns the intern table and its reference replacement.
                unsafe { pyo3::ffi::PyUnicode_InternInPlace(&mut pointer); }
                let interned = unsafe { Bound::from_owned_ptr_or_err(py, pointer)? };
                let changed = !interned.is(&*value);
                *value = interned;
                return Ok(changed);
            }
        } else if let Ok(tuple) = value.cast::<PyTuple>() {
            let mut items = tuple.iter().collect::<Vec<_>>();
            for item in items.iter_mut().rev() { Self::intern_literal(item)?; }
            *value = PyTuple::new(py, items)?.into_any();
            // CPython mutates nested tuples without rebuilding their parent set.
        }
        Ok(false)
    }

    fn is_literal(&self, py: Python<'_>) -> bool {
        match self {
            Self::Tuple(values) => values.iter().all(|value| value.is_literal(py)),
            Self::Atom(value) => {
                let value = value.bind(py);
                value.is_none() || value.is_exact_instance_of::<PyBool>() || value.is_exact_instance_of::<PyInt>()
                    || value.is_exact_instance_of::<PyString>()
                    || (value.is_exact_instance_of::<PyFloat>() && value.extract::<f64>().is_ok_and(f64::is_finite))
            }
            _ => false,
        }
    }

    fn materialize<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        match self {
            Self::Atom(value) => {
                let value = value.bind(py);
                if value.is_exact_instance_of::<PyFloat>() {
                    let number = value.extract::<f64>()?;
                    if !number.is_finite() {
                        return resolve_nonfinite(py, number);
                    }
                }
                Ok(value.clone())
            }
            Self::List(values) => Ok(PyList::new(py, values.iter().map(|v| v.materialize(py)).collect::<PyResult<Vec<_>>>()?)?.into_any()),
            Self::Tuple(values) => Ok(PyTuple::new(py, values.iter().map(|v| v.materialize(py)).collect::<PyResult<Vec<_>>>()?)?.into_any()),
            Self::Map(values) => {
                let result = PyDict::new(py);
                for (key, value) in values { result.set_item(key.materialize(py)?, value.materialize(py)?)?; }
                Ok(result.into_any())
            }
            Self::Set { values, frozen, compiled } => {
                if values.is_empty() {
                    return py.import("builtins")?.getattr(if *frozen { "frozenset" } else { "set" })?.call0();
                }
                let value = if let Some(compiled) = compiled {
                    py.get_type::<PySet>().call1((compiled.bind(py),))?
                } else {
                    PySet::new(py, values.iter().map(|v| v.materialize(py)).collect::<PyResult<Vec<_>>>()?)?.into_any()
                };
                if *frozen { py.import("builtins")?.getattr("frozenset")?.call1((value,)) }
                else { Ok(value) }
            }
        }
    }
}

struct ForeignConstantFactory { snapshot: ConstantSnapshot, bridge: Arc<Bridge> }
impl fmt::Debug for ForeignConstantFactory {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { f.write_str("ForeignConstantFactory") }
}

struct ContextFilterArgument {
    callback: Py<PyAny>,
    input: Py<PyAny>,
    bridge: Arc<Bridge>,
}

fn plain_extension_config<'py>(py: Python<'py>, config: &Bound<'py, PyAny>)
    -> PyResult<Option<Vec<Bound<'py, PyAny>>>> {
    let environment = config.getattr("environment")?;
    let environment_type = py.import("jinja2")?.getattr("Environment")?;
    if !environment.get_type().is(&environment_type)
        || !environment.getattr("finalize")?.is_none()
        || !environment.getattr("autoescape")?.is_exact_instance_of::<PyBool>()
        || !environment.getattr("globals")?.is_exact_instance_of::<PyDict>()
        || environment.getattr("is_async")?.is_truthy()?
        || !environment.getattr("line_statement_prefix")?.is_none()
        || !environment.getattr("line_comment_prefix")?.is_none() { return Ok(None); }
    for (name, expected) in [("variable_start_string", "{{"), ("variable_end_string", "}}"),
        ("block_start_string", "{%"), ("block_end_string", "%}"),
        ("comment_start_string", "{#"), ("comment_end_string", "#}"), ("newline_sequence", "\n")] {
        let value = environment.getattr(name)?;
        if !value.is_exact_instance_of::<PyString>() || value.extract::<String>()? != expected { return Ok(None); }
    }
    let extensions = environment.getattr("extensions")?;
    if !extensions.is_exact_instance_of::<PyDict>() { return Ok(None); }
    let extensions = extensions.cast_into::<PyDict>()?;
    if extensions.is_empty() { return Ok(None); }
    let base = py.import("jinja2.ext")?.getattr("Extension")?;
    let base_dict = base.getattr("__dict__")?;
    if base_dict.contains("__getattribute__")? || !base_dict.contains("filter_stream")?
        || !base_dict.contains("tags")? || !base_dict.contains("priority")? { return Ok(None); }
    let stream = base_dict.get_item("filter_stream")?;
    if !stream.is_exact_instance_of::<PyFunction>()
        || stream.getattr("__module__")?.extract::<String>().ok().as_deref() != Some("jinja2.ext")
        || stream.getattr("__qualname__")?.extract::<String>().ok().as_deref() != Some("Extension.filter_stream") { return Ok(None); }
    // Names can be forged when the base class is monkeypatched. Only skip the
    // default identity implementation on Python versions whose bytecode we
    // recognize; other implementations still run on the unsupported path.
    let code = stream.getattr("__code__")?;
    if code.getattr("co_argcount")?.extract::<u32>()? != 2
        || code.getattr("co_kwonlyargcount")?.extract::<u32>()? != 0
        || code.getattr("co_flags")?.extract::<u32>()? & 0x3a0 != 0
        || code.getattr("co_code")?.cast::<PyBytes>()?.as_bytes() != [0x97, 0, 0x7c, 1, 0x53, 0]
    { return Ok(None); }
    let tags = base_dict.get_item("tags")?;
    if !tags.is_exact_instance_of::<PySet>() || !tags.cast::<PySet>()?.is_empty() { return Ok(None); }
    let default_priority = base_dict.get_item("priority")?;
    if !default_priority.is_exact_instance_of::<PyInt>() { return Ok(None); }
    let Ok(default_priority) = default_priority.extract::<i64>() else { return Ok(None); };
    let mut result = Vec::with_capacity(extensions.len());
    for (_, extension) in extensions.iter() {
        let kind = extension.get_type();
        if !kind.get_type().is(&py.get_type::<PyType>()) { return Ok(None); }
        let bases = kind.getattr("__bases__")?.cast_into::<PyTuple>()?;
        if bases.len() != 1 || !bases.get_item(0)?.is(&base) { return Ok(None); }
        let class_dict = kind.getattr("__dict__")?;
        if class_dict.contains("__getattribute__")? || class_dict.contains("filter_stream")?
            || class_dict.contains("tags")?
            || class_dict.contains("__dict__")? { return Ok(None); }
        let class_priority = if class_dict.contains("priority")? {
            let value = class_dict.get_item("priority")?;
            if !value.is_exact_instance_of::<PyInt>() { return Ok(None); }
            Some(value)
        } else { None };
        let instance = extension.getattr("__dict__")?.cast_into::<PyDict>()?;
        if instance.contains("filter_stream")? || instance.contains("tags")? { return Ok(None); }
        let priority = if let Some(value) = instance.get_item("priority")? {
            value
        } else if let Some(value) = class_priority {
            value
        } else {
            result.push((default_priority, extension));
            continue;
        };
        if !priority.is_exact_instance_of::<PyInt>() { return Ok(None); }
        let Ok(priority) = priority.extract::<i64>() else { return Ok(None); };
        result.push((priority, extension));
    }
    // Python's stable priority sort retains registration order for equal keys.
    result.sort_by_key(|(priority, _)| *priority);
    Ok(Some(result.into_iter().map(|(_, extension)| extension).collect()))
}
impl fmt::Debug for ContextFilterArgument {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { f.write_str("ContextFilterArgument") }
}
impl Object for ContextFilterArgument {
    fn call(self: &Arc<Self>, state: &State, _args: &[Value]) -> Result<Value, Error> {
        Python::attach(|py| -> PyResult<Value> {
            let context = self.bridge.python(py, state.runtime_context())?;
            self.bridge.callbacks.fetch_add(1, Ordering::Relaxed);
            let value = crate::python_macro::with_state(&self.bridge, state, || self.callback.bind(py).call1((context, self.input.bind(py))))?;
            self.bridge.value(&value)
        }).map_err(|error| self.bridge.remember(error))
    }
}
impl Object for ForeignConstantFactory {
    fn call(self: &Arc<Self>, _state: &State, _args: &[Value]) -> Result<Value, Error> {
        Python::attach(|py| self.bridge.value(&self.snapshot.materialize(py)?))
            .map_err(|error| self.bridge.remember(error))
    }
}

struct SandboxOperatorFactory {
    bridge: Arc<Bridge>,
    token: Py<PyString>,
    unary: bool,
}

struct PreparedSandboxOperator {
    bridge: Arc<Bridge>,
    method: Py<PyAny>,
    context: Py<PyAny>,
    token: Py<PyString>,
    unary: bool,
}

impl fmt::Debug for SandboxOperatorFactory {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { f.write_str("SandboxOperatorFactory") }
}

impl fmt::Debug for PreparedSandboxOperator {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { f.write_str("PreparedSandboxOperator") }
}

impl Object for SandboxOperatorFactory {
    fn call(self: &Arc<Self>, state: &State, _args: &[Value]) -> Result<Value, Error> {
        Python::attach(|py| -> PyResult<Value> {
            let config = self.bridge.config.bind(py);
            let method = config.getattr("environment")?.getattr(if self.unary { "call_unop" } else { "call_binop" })?;
            let context = self.bridge.python(py, state.runtime_context())?;
            Ok(Value::from_object(PreparedSandboxOperator {
                bridge: self.bridge.clone(), method: method.unbind(), context,
                token: self.token.clone_ref(py), unary: self.unary,
            }))
        }).map_err(|error| self.bridge.remember(error))
    }
}

impl Object for PreparedSandboxOperator {
    fn call(self: &Arc<Self>, state: &State, args: &[Value]) -> Result<Value, Error> {
        Python::attach(|py| -> PyResult<Value> {
            let args = PyTuple::new(py, args.iter().map(|value| self.bridge.python(py, value)).collect::<PyResult<Vec<_>>>()?)?;
            let value = crate::python_macro::with_state(&self.bridge, state, || crate::python_sandbox::call_operator(self.method.bind(py), self.context.bind(py), self.token.bind(py), &args, self.unary))?;
            self.bridge.value(&value)
        }).map_err(|error| self.bridge.remember(error))
    }
}

pub struct Bridge {
    pub asynchronous: bool,
    pub(crate) await_adapter_probe: AtomicBool,
    pub config: Py<PyAny>,
    // Only the file-composition renderer owns a pre-materialized registry.
    // A standalone session passes Python values through without such a core.
    guard_unmaterialized_specials: bool,
    guard_reserved_literals: bool,
    // Retain the module, not its methods or guards: mutations stay observable.
    lookup_binding: Py<PyModule>,
    context: Py<PyAny>,
    plain_extension_candidate: bool,
    pub specials: Mutex<HashMap<usize, Value>>,
    pub lazy_images: Mutex<HashSet<usize>>,
    error: Mutex<Option<PyErr>>,
    globals: Mutex<HashSet<String>>,
    finalizer_plans: Mutex<HashMap<String, (bool, bool)>>,
    optimizer_plans: Mutex<HashMap<String, bool>>,
    constant_pools: Mutex<HashMap<String, Py<PyDict>>>,
    template_builtin_plans: Mutex<HashMap<String, TemplateSelection>>,
    pub callbacks: AtomicUsize,
    pub formats: AtomicUsize,
    pub fast_formats: AtomicUsize,
    this: std::sync::Weak<Bridge>,
}

impl Bridge {
    fn active_error_slot(&self) -> Option<LocalErrorSlot> {
        find_error_scope(self as *const Self as usize)
    }

    pub fn with_error_scope<R>(&self, operation: impl FnOnce() -> R) -> R {
        let slot = Rc::new(RefCell::new(None));
        self.with_error_slot(&slot, operation)
    }

    pub(crate) fn with_error_slot<R>(&self, slot: &LocalErrorSlot, operation: impl FnOnce() -> R) -> R {
        let bridge = self as *const Self as usize;
        push_error_scope(bridge, slot.clone());
        let _scope = ErrorScope { bridge };
        operation()
    }

    pub fn new(config: Py<PyAny>, context: Py<PyAny>, guard_unmaterialized_specials: bool,
        guard_reserved_literals: bool) -> PyResult<Arc<Self>> {
        let (plain_extension_candidate, asynchronous, lookup_binding) = Python::attach(|py| -> PyResult<_> {
            let asynchronous = config.bind(py).getattr("environment")?.getattr("is_async")?.is_truthy()?;
            let candidate = config.bind(py).getattr("_custom_extensions")?.is_truthy()?
                && !config.bind(py).getattr("is_async")?.is_truthy()?
                && context.bind(py).is_exact_instance_of::<PyDict>();
            Ok((candidate, asynchronous, py.import("docxtpl_native.bridge")?.unbind()))
        })?;
        Ok(Arc::new_cyclic(|this| Self { this: this.clone(), config, context, guard_unmaterialized_specials, guard_reserved_literals, lookup_binding, plain_extension_candidate, asynchronous, await_adapter_probe: AtomicBool::new(false), specials: Mutex::new(HashMap::new()), lazy_images: Mutex::new(HashSet::new()),
            error: Mutex::new(None), globals: Mutex::new(HashSet::new()),
            finalizer_plans: Mutex::new(HashMap::new()), optimizer_plans: Mutex::new(HashMap::new()),
            constant_pools: Mutex::new(HashMap::new()), template_builtin_plans: Mutex::new(HashMap::new()),
            callbacks: AtomicUsize::new(0), formats: AtomicUsize::new(0), fast_formats: AtomicUsize::new(0) }))
    }

    // Not inlined: called after a fiber may have resumed on another thread.
    #[inline(never)]
    pub fn remember(&self, error: PyErr) -> Error {
        // The Python exception is re-raised at the binding boundary. Formatting
        // it here would call user __str__ code that the reference never calls.
        if let Some(active) = self.active_error_slot() {
            let mut slot = active.borrow_mut();
            if slot.is_none() { *slot = Some(error); }
        } else {
            let mut slot = self.error.lock().unwrap();
            if slot.is_none() { *slot = Some(error); }
        }
        Error::new(ErrorKind::InvalidOperation, "Python exception retained by the binding")
    }

    #[inline(never)]
    pub fn take_error(&self) -> Option<PyErr> {
        match self.active_error_slot() {
            Some(slot) => slot.borrow_mut().take(),
            None => self.error.lock().unwrap().take(),
        }
    }

    pub(crate) fn peek_error(&self, py: Python<'_>) -> Option<PyErr> {
        match self.active_error_slot() {
            Some(slot) => slot.borrow().as_ref().map(|error| error.clone_ref(py)),
            None => self.error.lock().unwrap().as_ref().map(|error| error.clone_ref(py)),
        }
    }

    pub(crate) fn replace_error(&self, error: PyErr) {
        let previous = match self.active_error_slot() {
            Some(slot) => slot.borrow_mut().replace(error),
            None => self.error.lock().unwrap().replace(error),
        };
        drop(previous);
    }

    pub(crate) fn traceback_locals(&self, py: Python<'_>, state: &State) -> PyResult<Option<Py<PyAny>>> {
        let bindings = state.error_frame_bindings();
        if bindings.iter().any(|(_, value)| Self::needs_native_scope(value)
            && value.downcast_object_ref::<PreparedMissingRoot>().is_none()) {
            return Ok(None);
        }
        let context = self.python(py, state.runtime_context())?;
        let locals = crate::python_context_helpers::python_context_get_all(py, context.bind(py))?.call_method0("copy")?;
        let missing = py.import("jinja2.runtime")?.getattr("missing")?;
        for (name, value) in bindings {
            if value.is_undefined() || value.downcast_object_ref::<PreparedMissingRoot>().is_some() { continue; }
            let value = self.python(py, &value)?;
            if !value.bind(py).is(&missing) { locals.set_item(name, value)?; }
        }
        Ok(Some(locals.unbind()))
    }

    fn compatible_unpack_error(&self, py: Python<'_>) -> bool {
        match self.active_error_slot() {
            Some(slot) => slot.borrow().as_ref().is_none_or(|error| error.is_instance_of::<PyTypeError>(py)),
            None => self.error.lock().unwrap().as_ref().is_none_or(|error| error.is_instance_of::<PyTypeError>(py)),
        }
    }

    fn replace_unpack_error(&self, py: Python<'_>, error: PyErr) {
        let previous = match self.active_error_slot() {
            Some(slot) => {
                let mut slot = slot.borrow_mut();
                if slot.as_ref().is_none_or(|value| value.is_instance_of::<PyTypeError>(py)) { slot.replace(error) }
                else { None }
            }
            None => {
                let mut slot = self.error.lock().unwrap();
                if slot.as_ref().is_none_or(|value| value.is_instance_of::<PyTypeError>(py)) { slot.replace(error) }
                else { None }
            }
        };
        drop(previous);
    }

    /// Apply user preprocessing hooks to plain text without using the
    /// reference template parser or renderer. Anything outside this narrow
    /// contract keeps the normal explicit unsupported-extension path.
    pub fn preprocess_plain_extension(&self, source: &str) -> PyResult<Option<String>> {
        fn is_plain(source: &str) -> bool {
            !source.contains("{{") && !source.contains("{%") && !source.contains("{#")
                && !source.contains('\r') && !source.ends_with('\n')
        }
        if !self.plain_extension_candidate || !is_plain(source) { return Ok(None); }
        Python::attach(|py| {
            let config = self.config.bind(py);
            let Some(extensions) = plain_extension_config(py, config)? else { return Ok(None); };
            // Jinja selects from_code before it runs extension preprocessing.
            let _selection = template_builtins(py, self)?;
            let mut result = PyString::new(py, source).into_any();
            for extension in &extensions {
                result = extension.call_method1("preprocess", (&result, py.None(), py.None()))?;
            }
            if !result.is_exact_instance_of::<PyString>() { return Err(unsupported(py, "extension preprocessing returned non-string output")); }
            let text = result.extract::<String>()?;
            let still_supported = plain_extension_config(py, config)?
                .is_some_and(|current| current.len() == extensions.len()
                    && current.iter().zip(&extensions).all(|(a, b)| a.is(b)));
            if !is_plain(&text) || !still_supported {
                return Err(unsupported(py, "extension preprocessing requires a native template adapter"));
            }
            Ok(Some(text))
        })
    }

    pub fn snapshot(self: &Arc<Self>) -> PyResult<Value> { self.snapshot_with_blocks(None) }

    /// The root context; with the template's blocks, its `blocks` is the
    /// registry the render uses (see python_blocks.rs).
    pub fn snapshot_with_blocks(self: &Arc<Self>, blocks: Option<crate::python_blocks::RootBlocks>) -> PyResult<Value> {
        Python::attach(|py| {
            let namespace = py.get_type::<PyDict>().call1((self.context.bind(py),))?.cast_into::<PyDict>()?;
            let environment = self.config.bind(py).getattr("environment")?;
            let registry = match &blocks {
                Some(blocks) => crate::python_blocks::root_blocks(py, self, blocks)?,
                None => PyDict::new(py),
            };
            let context = crate::python_context_helpers::new_context(py,
                &py.import("jinja2.environment")?.getattr("new_context")?, &environment,
                py.None().bind(py), &registry, namespace.as_any(), false,
                &environment.getattr("globals")?, py.None().bind(py))?;
            if blocks.is_some() { crate::python_blocks::mark_registry(py, &context)?; }
            self.root_value(py, &context, &environment)
        })
    }

    /// The template root for an existing Python context.
    pub(crate) fn root_value(self: &Arc<Self>, py: Python<'_>, context: &Bound<'_, PyAny>,
        environment: &Bound<'_, PyAny>) -> PyResult<Value> {
        // These two lookups are the prologue of Jinja's root generator,
        // not part of new_context. Preserve its escaping exception rules.
        let boundary = if self.asynchronous { crate::python_errors::Boundary::AsyncGenerator }
            else { crate::python_errors::Boundary::Generator };
        let prepared_resolver = context.getattr("resolve_or_missing")
            .map_err(|error| crate::python_errors::escaped_iteration(py, error, boundary))?.unbind();
        let prepared_undefined = environment.getattr("undefined")
            .map_err(|error| crate::python_errors::escaped_iteration(py, error, boundary))?.unbind();
        Ok(Value::from_object(PythonObject { object: context.getattr("parent")?.unbind(),
            activation: Some(context.clone().unbind()), fallbacks: None,
            bridge: self.clone(), root: true, undefined: Some(false), prepared_resolver: Some(prepared_resolver), prepared_undefined: Some(prepared_undefined), text: None, special: None, dynamic_image: false, special_classes: None }))
    }

    pub fn value(self: &Arc<Self>, value: &Bound<'_, PyAny>) -> PyResult<Value> {
        if let Some(special) = self.specials.lock().unwrap().get(&(value.as_ptr() as usize)).cloned() {
            return Ok(Value::from_object(PythonObject { object: value.clone().unbind(),
                bridge: self.clone(), root: false, activation: None, fallbacks: None,
                undefined: Some(false), prepared_resolver: None, prepared_undefined: None, text: None, special: Some(special), dynamic_image: false, special_classes: None }));
        }
        if value.is_none() { return Ok(Value::from(())); }
        if value.is_exact_instance_of::<PyBool>() { return Ok(Value::from(value.extract::<bool>()?)); }
        if value.is_exact_instance_of::<PyInt>() || value.is_exact_instance_of::<PyFloat>()
            || value.is_exact_instance_of::<PyString>() || value.is_exact_instance_of::<PyBytes>() {
            // Preserve the original scalar object, including identity and
            // arbitrary-precision numeric protocols across Python callbacks.
            return Ok(Value::from_object(PythonObject { object: value.clone().unbind(),
                bridge: self.clone(), root: false, activation: None, fallbacks: None, undefined: Some(false), prepared_resolver: None, prepared_undefined: None,
                text: if value.is_exact_instance_of::<PyString>() { value.extract::<String>().ok() } else { None }, special: None, dynamic_image: false, special_classes: None }));
        }
        let py = value.py();
        let mut dynamic_image = false;
        let mut special_classes = None;
        if self.guard_unmaterialized_specials && !self.config.bind(py).getattr("package_values_live")?.is_truthy()? {
            let classes = current_special_classes(py)?;
            dynamic_image = self.guard_special(value, &classes)?;
            special_classes = Some(Arc::new(classes));
        }
        Ok(Value::from_object(PythonObject { object: value.clone().unbind(),
            // A Python object's reported class can be dynamic and its
            // __class__ lookup observable. Ask when an undefined operation
            // needs the answer, rather than while merely wrapping the value.
            bridge: self.clone(), root: false, activation: None, fallbacks: None, undefined: None, prepared_resolver: None, prepared_undefined: None, text: None, special: None, dynamic_image, special_classes }))
    }

    /// Reject an image or subdocument that entered the engine without being
    /// registered, unless the engine can materialize it. Returns whether the
    /// value is a package image the callback scope materializes on output.
    fn guard_special(&self, value: &Bound<'_, PyAny>, classes: &SpecialClasses) -> PyResult<bool> {
        let py = value.py();
        let (package, image, subdoc) = (classes.package.bind(py), classes.image.bind(py), classes.subdoc.bind(py));
        let mut dynamic_image = false;
        let mut guarded_native_image = false;
        let special = match crate::live_routing::default_special_types(package, image, subdoc)? {
            Some(classes) => {
                guarded_native_image = unsafe {
                    pyo3::ffi::PyType_IsSubtype(value.get_type().as_type_ptr(), image.cast::<PyType>()?.as_type_ptr()) != 0
                } && docxtpl_core::has_unowned_callback_image_scope()
                    && crate::inline_image::unregistered_protocol_is_standard(value, package)?;
                dynamic_image = docxtpl_core::has_owned_callback_image_scope() && unsafe {
                    pyo3::ffi::PyType_IsSubtype(value.get_type().as_type_ptr(), classes.0.as_type_ptr()) != 0
                };
                crate::live_routing::actual_special_type(value, &classes)
            },
            None => value.is_instance(image)? || value.is_instance(subdoc)?,
        };
        if special && !dynamic_image && !guarded_native_image {
            return Err(unsupported(py, "XML/image values returned lazily need a native materialization hook"));
        }
        Ok(dynamic_image)
    }

    /// Check callback arguments again when the package's InlineImage or Subdoc
    /// changed after they entered the engine, so a class replaced during the
    /// render decides what reaches the callback. Reading the package goes
    /// through the import system: a missing entry runs the import hook and a
    /// None entry raises.
    fn recheck_arguments(&self, py: Python<'_>, values: &[&Value]) -> PyResult<()> {
        let checked: Vec<_> = values.iter().filter_map(|value| value.downcast_object_ref::<PythonObject>())
            .filter(|object| object.special.is_none() && object.special_classes.is_some()).collect();
        if checked.is_empty() || self.config.bind(py).getattr("package_values_live")?.is_truthy()? {
            return Ok(());
        }
        let current = current_special_classes(py)?;
        for object in checked {
            let Some(previous) = &object.special_classes else { continue };
            if previous.image.bind(py).is(current.image.bind(py)) && previous.subdoc.bind(py).is(current.subdoc.bind(py)) {
                continue;
            }
            self.guard_special(object.object.bind(py), &current)?;
        }
        Ok(())
    }

    fn unpacked_value(self: &Arc<Self>, value: &Bound<'_, PyAny>) -> PyResult<Value> {
        let registered = self.specials.lock().unwrap().contains_key(&(value.as_ptr() as usize));
        if registered { return self.value(value); }
        if value.is_none() || value.is_exact_instance_of::<PyBool>()
            || value.is_exact_instance_of::<PyInt>() || value.is_exact_instance_of::<PyFloat>()
            || value.is_exact_instance_of::<PyString>() || value.is_exact_instance_of::<PyBytes>() {
            return self.value(value);
        }
        if !self.config.bind(value.py()).getattr("package_values_live")?.is_truthy()? {
            return self.value(value);
        }
        // Binding a target must not inspect an arbitrary value that the body
        // may never use. Undefined checks happen at the actual test/filter.
        Ok(self.opaque_value(value))
    }

    /// Keep an argument opaque until its callee actually requests a protocol.
    pub(crate) fn opaque_value(self: &Arc<Self>, value: &Bound<'_, PyAny>) -> Value {
        Value::from_object(PythonObject { object: value.clone().unbind(),
            bridge: self.clone(), root: false, activation: None, fallbacks: None, undefined: None, prepared_resolver: None, prepared_undefined: None, text: None, special: None, dynamic_image: false, special_classes: None })
    }

    pub fn python(&self, py: Python<'_>, value: &Value) -> PyResult<Py<PyAny>> {
        if let Some(missing) = value.downcast_object_ref::<PreparedMissingRoot>() {
            return missing.materialize(py);
        }
        if let Some(value) = value.downcast_object_ref::<minijinja::RuntimeMacro>().and_then(|value| value.foreign_value()) {
            return self.python(py, value);
        }
        if let Some(object) = value.downcast_object_ref::<PythonObject>() {
            return Ok(object.object.clone_ref(py));
        }
        let module = value.downcast_object::<minijinja::RuntimeModule>().zip(self.this.upgrade());
        if let Some((module, bridge)) = module {
            return crate::python_module::foreign_module(py, &bridge, &module);
        }
        // A loop crossing into Python becomes its live LoopContext.
        let live = match value.downcast_object::<minijinja::RuntimeLoop>() {
            Some(source) => match self.this.upgrade() {
                Some(bridge) => crate::python_loop::live_loop(py, &bridge, &source)?,
                None => None,
            },
            None => None,
        };
        if let Some(object) = live { return Ok(object); }
        match value.kind() {
            ValueKind::None => Ok(py.None()),
            ValueKind::Bool => value.is_true().into_py_any(py),
            ValueKind::Number => {
                if value.is_integer() {
                    if let Ok(number) = i128::try_from(value.clone()) { number.into_py_any(py) }
                    else { u128::try_from(value.clone()).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?.into_py_any(py) }
                } else {
                    f64::try_from(value.clone()).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?.into_py_any(py)
                }
            }
            ValueKind::String => {
                let text = value.as_str().unwrap();
                if value.is_safe() { Ok(py.import("markupsafe")?.getattr("Markup")?.call1((text,))?.unbind()) }
                else { text.into_py_any(py) }
            }
            ValueKind::Bytes => Ok(PyBytes::new(py, value.as_bytes().unwrap()).into_any().unbind()),
            ValueKind::Undefined => match value.silent_undefined_line() {
                // As Jinja's cond_expr_undefined: a plain Undefined, whatever
                // the environment's class.
                Some(line) => {
                    let hint = format!(
                        "the inline if-expression on line {line} evaluated to false and no else section was defined."
                    );
                    let undefined = py.import("jinja2.runtime")?.getattr("Undefined")?;
                    let options = pyo3::types::PyDict::new(py);
                    options.set_item("hint", hint)?;
                    Ok(undefined.call((), Some(&options))?.unbind())
                }
                None => Ok(self.config.bind(py).call_method1("missing", (py.None(), "unknown", true))?.unbind()),
            },
            _ => {
                let object = value.as_object().ok_or_else(|| unsupported(py, "native value cannot cross the Python bridge"))?;
                match object.repr() {
                    ObjectRepr::Map => {
                        let result = PyDict::new(py);
                        if let Some(pairs) = object.try_iter_pairs() {
                            for (key, item) in pairs { result.set_item(self.python(py, &key)?, self.python(py, &item)?)?; }
                        }
                        Ok(result.into_any().unbind())
                    }
                    ObjectRepr::Seq | ObjectRepr::Iterable => {
                        let result = PyList::empty(py);
                        if let Some(items) = object.try_iter() {
                            for item in items { result.append(self.python(py, &item)?)?; }
                        }
                        Ok(result.into_any().unbind())
                    }
                    _ => Err(unsupported(py, "native callable/loop objects need a Python runtime adapter")),
                }
            }
        }
    }

    fn needs_native_scope(value: &Value) -> bool {
        value.downcast_object_ref::<PythonObject>().is_none()
            && value.downcast_object_ref::<minijinja::RuntimeMacro>().is_none()
            && value.as_object().is_some_and(|object| !matches!(object.repr(), ObjectRepr::Seq))
    }

    fn visible(&self, py: Python<'_>, state: &State) -> PyResult<Py<PyDict>> {
        let result = if state.context_base().downcast_object_ref::<PythonObject>()
            .is_some_and(|object| object.root) {
            let context = self.python(py, state.runtime_context())?;
            py.get_type::<PyDict>().call1((crate::python_context_helpers::python_context_get_all(py, context.bind(py))?,))?.cast_into::<PyDict>()?
        } else { PyDict::new(py) };
        for name in state.callback_known_variables() {
            if let Some(value) = state.callback_lookup(&name) {
                // Inspecting a native loop object's fields can consume its
                // lookahead iterator (last/nextitem/length). Snapshotting the
                // callback context must not itself advance user generators.
                // A real Python LoopContext/Namespace adapter is still needed.
                if Self::needs_native_scope(&value) {
                    continue;
                }
                // Native builtins remain represented by the original Python
                // environment globals in BridgeConfig.context.
                if let Ok(value) = self.python(py, &value) { result.set_item(name.as_ref(), value)?; }
            }
        }
        Ok(result.unbind())
    }

    fn loop_arguments<'py>(self: &Arc<Self>, py: Python<'py>, state: &State, args: &[Value])
        -> PyResult<(Vec<Value>, Option<Bound<'py, PyDict>>)> {
        let mut positional = args.to_vec();
        let kwargs = if positional.last().is_some_and(Value::is_kwargs) {
            let keyword_value = positional.pop().unwrap();
            let prepared = match state.prepared_call() {
                Some(value) => self.python(py, &value)?,
                None => self.python(py, state.runtime_context())?.bind(py).getattr("call")?.unbind(),
            };
            let kwargs = if let Some(sources) = keyword_value.keyword_sources() {
                let mappings = sources.iter().map(|value| self.python(py, value)).collect::<PyResult<Vec<_>>>()?;
                let mappings = PyList::new(py, mappings)?;
                let implicit_name = match state.runtime_scope() {
                    minijinja::RuntimeScope::Loop => Some("_loop_vars"),
                    minijinja::RuntimeScope::Block => Some("_block_vars"),
                    _ => None,
                };
                if let Some(name) = implicit_name {
                    let implicit = PyDict::new(py);
                    implicit.set_item(name, PyDict::new(py))?;
                    mappings.insert(0, implicit)?;
                }
                crate::python_keywords::merge(prepared.bind(py), &mappings)?
            } else {
                let kwargs = PyDict::new(py);
                for key in keyword_value.try_iter().map_err(|error| PyValueError::new_err(error.to_string()))? {
                    let value = keyword_value.get_item(&key).map_err(|error| PyValueError::new_err(error.to_string()))?;
                    kwargs.set_item(self.python(py, &key)?, self.python(py, &value)?)?;
                }
                kwargs
            };
            crate::python_keywords::snapshot(&kwargs)?;
            let kwargs = if let Ok(function) = prepared.bind(py).getattr("__func__") {
                crate::python_call::bound_keywords(&function, &kwargs)?
            } else { kwargs };
            Some(kwargs)
        } else { None };
        Ok((positional, kwargs))
    }

    pub fn invoke(self: &Arc<Self>, callback: &Bound<'_, PyAny>, state: &State, args: &[Value], trusted: bool) -> PyResult<Value> {
        // A loop reached by the callback can evaluate its predicates in this render.
        minijinja::with_host_state(state, || self.invoke_in_state(callback, state, args, trusted))
    }

    fn invoke_in_state(self: &Arc<Self>, callback: &Bound<'_, PyAny>, state: &State, args: &[Value], trusted: bool) -> PyResult<Value> {
        let py = callback.py();
        let call_policy = state.take_call_policy();
        let prepared = state.take_prepared_call().map(|value| self.python(py, &value)).transpose()?;
        let positional = PyList::empty(py);
        let keywords = PyDict::new(py);
        let mut sources = None;
        if self.guard_unmaterialized_specials {
            let mut values: Vec<&Value> = Vec::with_capacity(args.len());
            let mut owned = Vec::new();
            for arg in args {
                if let Some(originals) = arg.keyword_sources() { values.extend(originals.iter()); }
                else if arg.is_kwargs() {
                    for key in arg.try_iter().map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))? {
                        owned.push(arg.get_item(&key).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?);
                    }
                } else { values.push(arg); }
            }
            values.extend(owned.iter());
            self.recheck_arguments(py, &values)?;
        }
        for arg in args {
            if let Some(originals) = arg.keyword_sources() {
                let values = originals.iter().map(|value| self.python(py, value)).collect::<PyResult<Vec<_>>>()?;
                sources = Some(PyList::new(py, values)?);
            } else if arg.is_kwargs() {
                for key in arg.try_iter().map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))? {
                    let value = arg.get_item(&key).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
                    keywords.set_item(self.python(py, &key)?, self.python(py, &value)?)?;
                }
            } else { positional.append(self.python(py, arg)?)?; }
        }
        if !trusted {
            let key = match state.runtime_scope() {
                minijinja::RuntimeScope::Loop => Some("_loop_vars"),
                minijinja::RuntimeScope::Block => Some("_block_vars"),
                _ => None,
            };
            if let Some(key) = key {
                let locals = PyDict::new(py);
                for (name, value) in state.runtime_locals() { locals.set_item(name, self.python(py, &value)?)?; }
                keywords.set_item(key, &locals)?;
                if let Some(sources) = &sources {
                    let implicit = PyDict::new(py);
                    implicit.set_item(key, locals)?;
                    sources.insert(0, implicit)?;
                }
            }
        }
        self.callbacks.fetch_add(1, Ordering::Relaxed);
        let context = self.python(py, state.runtime_context())?;
        let result = crate::python_macro::with_state(self, state, || crate::python_call::invoke_keywords(py, self.config.bind(py), callback,
            positional.as_any(), &keywords, py.None().bind(py),
            state.auto_escape() != AutoEscape::None, trusted, sources.as_ref(), Some(context.bind(py)), call_policy,
            prepared.as_ref().map(|value| value.bind(py))))?;
        self.value(&result)
    }
}

/// The package and its public InlineImage and Subdoc classes at one moment.
pub(crate) struct SpecialClasses { package: Py<PyAny>, image: Py<PyAny>, subdoc: Py<PyAny> }

/// Read the package from sys.modules, or import it when the entry is missing
/// or None, and its current public special classes.
fn current_special_classes(py: Python<'_>) -> PyResult<SpecialClasses> {
    let package = match crate::find_loaded_module(py, pyo3::intern!(py, "docxtpl_native"))? {
        Some(package) if !package.bind(py).is_none() => package.into_bound(py),
        _ => py.import("docxtpl_native")?.into_any(),
    };
    let image = package.getattr("InlineImage")?.unbind();
    let subdoc = package.getattr("Subdoc")?.unbind();
    Ok(SpecialClasses { package: package.unbind(), image, subdoc })
}

pub(crate) fn unsupported(py: Python<'_>, message: &str) -> PyErr {
    match py.import("docxtpl_native").and_then(|module| module.getattr("UnsupportedFeature"))
        .and_then(|class| class.call1((message,))) {
        Ok(value) => PyErr::from_value(value),
        Err(error) => error,
    }
}

pub fn property_value_error(context: &Value, message: String) -> String {
    if let Some(object) = context.downcast_object_ref::<PythonObject>() {
        object.bridge.remember(pyo3::exceptions::PyValueError::new_err(message)).to_string()
    } else { message }
}

unsafe extern "C" {
    static mut PyStaticMethod_Type: pyo3::ffi::PyTypeObject;
    static mut PyClassMethod_Type: pyo3::ffi::PyTypeObject;
}

/// Whether `member` is exactly a staticmethod or classmethod object.
pub(crate) fn is_static_or_class_method(member: &Bound<'_, PyAny>) -> bool {
    let kind = unsafe { pyo3::ffi::Py_TYPE(member.as_ptr()) };
    kind == &raw mut PyStaticMethod_Type || kind == &raw mut PyClassMethod_Type
}

// Only built-in static/class descriptors can be bound from the class without
// passing the wrong receiver to a user-defined descriptor.
pub(crate) fn select_async_to_iterator<'py>(binding: &Bound<'py, PyModule>, class: &Bound<'py, PyAny>)
    -> PyResult<(Bound<'py, PyAny>, bool)> {
    if !class.is_exact_instance_of::<PyType>() { return Ok((class.getattr("_to_iterator")?, false)); }
    let dictionary = class.getattr("__dict__")?;
    let current = match dictionary.get_item("_to_iterator") {
        Ok(value) => value,
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyKeyError>(binding.py()) => {
            return Ok((class.getattr("_to_iterator")?, false));
        }
        Err(error) => return Err(error),
    };
    let kind = unsafe { pyo3::ffi::Py_TYPE(current.as_ptr()) };
    let static_method = kind == &raw mut PyStaticMethod_Type;
    let class_method = kind == &raw mut PyClassMethod_Type;
    if !static_method && !class_method { return Ok((class.getattr("_to_iterator")?, false)); }
    let method = class.getattr("_to_iterator")?;
    if class_method || !method.is_exact_instance_of::<PyFunction>() { return Ok((method, true)); }
    let code = method.getattr("__code__")?;
    let name = code.getattr("co_name")?;
    if name.is_exact_instance_of::<PyString>() && !name.eq("_to_iterator")? { return Ok((method, true)); }
    // A function other than the one the class held when the extension was
    // loaded (an exact code clone included) is a user callback.
    let original = crate::python_async::is_original_async_to_iterator(&method)?;
    Ok((method, !original))
}

/// `AsyncLoopContext._to_iterator(values)` for the list that `length`
/// materializes, built here when the method, the `auto_aiter` it calls and
/// the adapter class are the originals: `_IteratorToAsyncIterator(iter(values))`.
fn standard_to_iterator<'py>(binding: &Bound<'py, PyModule>, class: &Bound<'py, PyAny>, values: &Bound<'py, PyList>)
    -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = binding.py();
    let (method, changed) = select_async_to_iterator(binding, class)?;
    if changed || !method.is_exact_instance_of::<PyFunction>() { return Ok(None); }
    let mut original = false;
    for pair in binding.getattr("_RUNTIME_ASYNC_LOOP_CLASS_CODES")?.try_iter()? {
        let (function, code) = pair?.extract::<(Bound<'py, PyAny>, Bound<'py, PyAny>)>()?;
        if function.is(&method) && method.getattr("__code__")?.is(&code)
            && method.getattr("__name__")?.eq("_to_iterator")? { original = true; }
    }
    if !original { return Ok(None); }
    let scope = crate::pyglobals::FrameGlobals::of_function(&method)?;
    let auto_aiter = crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "auto_aiter"))?;
    if !crate::python_async::is_standard_auto_aiter(&auto_aiter)? { return Ok(None); }
    let utils = crate::pyglobals::FrameGlobals::of_function(&auto_aiter)?;
    let builtins = py.import("builtins")?;
    for name in ["hasattr", "iter"] {
        let name = PyString::new(py, name);
        if !crate::pyglobals::frame_global(&utils, &name)?.is(&builtins.getattr(&name)?) { return Ok(None); }
    }
    let adapter = crate::pyglobals::frame_global(&utils, pyo3::intern!(py, "_IteratorToAsyncIterator"))?;
    if !adapter.is_exact_instance_of::<PyType>() || !PythonIterator::adapter_constructor_is_standard(&adapter)? {
        return Ok(None);
    }
    // A list has no __aiter__: auto_aiter wraps iter(values).
    let iterator = values.try_iter()?;
    let wrapper = unsafe { Bound::from_owned_ptr_or_err(py,
        pyo3::ffi::PyType_GenericAlloc(adapter.cast::<PyType>()?.as_type_ptr(), 0))? };
    wrapper.setattr(pyo3::intern!(py, "_iterator"), iterator)?;
    Ok(Some(wrapper))
}

#[derive(Clone, Copy)]
enum IteratorMode { Synchronous, SynchronousLoop, Adapted { with_metadata: bool }, AdaptedObject { with_metadata: bool }, Async { with_metadata: bool } }
pub(crate) struct PythonIterator { iterator: Py<PyAny>, bridge: Arc<Bridge>, done: bool, mode: IteratorMode, after: Option<Py<PyAny>>, async_stop: Option<PyErr>, adapted_class: Option<Py<PyAny>> }

impl PythonIterator {
    fn finish_async_materialization(&mut self, values: &Bound<'_, PyList>) -> PyResult<(Box<dyn minijinja::value::LoopIterator>, Option<Value>)> {
        let py = values.py();
        let binding = crate::python_function::binding(py)?;
        let class = binding.getattr("_RUNTIME_ASYNC_LOOP_TYPE")?;
        let replacement = match standard_to_iterator(&binding, &class, values)? {
            Some(replacement) => replacement,
            None => class.getattr("_to_iterator")?.call1((values,))?,
        };
        let length = class.getattr("length")?.getattr("fget")?;
        let scope = crate::pyglobals::FrameGlobals::of_function(&length)?;
        let count = crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "len"))?.call1((values,))?;
        let count = self.bridge.value(&count)?;
        let same_adapter = if let Some(class) = &self.adapted_class {
            replacement.get_type().is(class.bind(py))
        } else if matches!(self.mode, IteratorMode::AdaptedObject { .. }) {
            replacement.get_type().is(&self.iterator.bind(py).get_type())
        } else { false };
        let mode = if same_adapter {
            IteratorMode::AdaptedObject { with_metadata: true }
        } else {
            IteratorMode::Async { with_metadata: true }
        };
        Ok((Box::new(Self { iterator: replacement.unbind(), bridge: self.bridge.clone(), done: false,
            mode, after: self.after.take(), async_stop: None, adapted_class: None }), Some(count)))
    }

    fn managed(&self) -> bool {
        matches!(self.mode, IteratorMode::SynchronousLoop | IteratorMode::Adapted { with_metadata: true } | IteratorMode::AdaptedObject { with_metadata: true } | IteratorMode::Async { with_metadata: true })
    }

    fn after_and_missing<'py>(&self, py: Python<'py>, method_name: &str) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
        let binding = crate::python_function::binding(py)?;
        let asynchronous = matches!(self.mode, IteratorMode::Adapted { .. } | IteratorMode::AdaptedObject { .. } | IteratorMode::Async { .. });
        let class = binding.getattr(if asynchronous { "_RUNTIME_ASYNC_LOOP_TYPE" } else { "_RUNTIME_LOOP_TYPE" })?;
        let method = class.getattr(method_name)?;
        let scope = crate::pyglobals::FrameGlobals::of_function(&method)?;
        let after = match &self.after { Some(value) => value.bind(py).clone(), None => class.getattr("_after")? };
        let missing = crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "missing"))?;
        Ok((after, missing))
    }

    fn take_cached(&mut self) -> PyResult<Option<Py<PyAny>>> {
        Python::attach(|py| {
            let name = if matches!(self.mode, IteratorMode::SynchronousLoop) { "__next__" } else { "__anext__" };
            let (after, missing) = self.after_and_missing(py, name)?;
            if after.is(&missing) { return Ok(None); }
            self.after = Some(missing.unbind());
            Ok(Some(after.unbind()))
        })
    }

    fn next_async(&mut self, with_metadata: bool, preserve_stop: bool) -> PyResult<Option<Py<PyAny>>> {
        let next = Python::attach(|py| -> PyResult<Py<PyAny>> {
            let iterator = self.iterator.bind(py);
            let value = if with_metadata { iterator.call_method0("__anext__")? } else {
                let Some(slot) = crate::python_async::async_slot(iterator, pyo3::ffi::Py_am_anext) else {
                    return Err(PyTypeError::new_err(format!("'async for' requires an iterator with __anext__ method, got {}", iterator.get_type().name()?)));
                };
                unsafe { Bound::from_owned_ptr_or_err(py, slot(iterator.as_ptr()))? }
            };
            match crate::python_async::await_iterator(&value) {
                Ok(iterator) => Ok(iterator),
                Err(error) if !with_metadata => {
                    let wrapped = PyTypeError::new_err(format!("'async for' received an invalid object from __anext__: {}", value.get_type().name()?));
                    wrapped.set_cause(py, Some(error));
                    Err(wrapped)
                }
                Err(error) => Err(error),
            }
        }).and_then(crate::python_async::drive);
        // After the await, possibly on another thread (see fiber_local).
        crate::fiber_local::attach(|py| match next {
            Ok(value) => Ok(Some(value)),
            Err(error) if error.is_instance_of::<pyo3::exceptions::PyStopAsyncIteration>(py) => {
                if preserve_stop { self.async_stop = Some(error); }
                Ok(None)
            },
            Err(error) => { self.done = true; Err(error) }
        })
    }

    fn adapted_type(py: Python<'_>, selected: Option<&Bound<'_, PyAny>>) -> PyResult<Py<PyAny>> {
        let auto_aiter = if let Some(selected) = selected { selected.clone() } else {
            let binding = crate::python_function::binding(py)?;
            let class = binding.getattr("_RUNTIME_ASYNC_LOOP_TYPE")?;
            let to_iterator = class.getattr("_to_iterator")?;
            let scope = crate::pyglobals::FrameGlobals::of_function(&to_iterator)?;
            crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "auto_aiter"))?
        };
        let scope = crate::pyglobals::FrameGlobals::of_function(&auto_aiter)?;
        let class = crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "_IteratorToAsyncIterator"))?;
        if !class.is_exact_instance_of::<PyType>() {
            return Err(unsupported(py, "custom async iterator class needs a runtime adapter"));
        }
        Ok(class.unbind())
    }

    pub(crate) fn adapter_constructor_is_standard(class: &Bound<'_, PyAny>) -> PyResult<bool> {
        let py = class.py();
        let binding = crate::python_function::binding(py)?;
        if !class.is(&binding.getattr("_ASYNC_ADAPTER_TYPE")?)
            || !class.get_type().is(&py.get_type::<PyType>())
            || !class.getattr("__new__")?.is(&binding.getattr("_OBJECT_NEW")?) {
            return Ok(false);
        }
        let method = class.getattr("__init__")?;
        let original = binding.getattr("_ASYNC_ADAPTER_INIT")?;
        let code = binding.getattr("_ASYNC_ADAPTER_INIT_CODE")?;
        let module = binding.getattr("_ASYNC_UTILS_MODULE")?;
        crate::python_function::is_standard_function(
            &method, &original, &code, module.cast::<pyo3::types::PyModule>()?, "__init__")
    }

    fn adapter_aiter_is_standard(class: &Bound<'_, PyAny>) -> PyResult<bool> {
        let py = class.py();
        let binding = crate::python_function::binding(py)?;
        let method = class.getattr("__aiter__")?;
        let original = binding.getattr("_ASYNC_ADAPTER_AITER")?;
        let code = binding.getattr("_ASYNC_ADAPTER_AITER_CODE")?;
        let module = binding.getattr("_ASYNC_UTILS_MODULE")?;
        crate::python_function::is_standard_function(
            &method, &original, &code, module.cast::<pyo3::types::PyModule>()?, "__aiter__")
    }

    fn adapted_next_for_lookahead(&mut self) -> PyResult<Option<Py<PyAny>>> {
        let switched = Python::attach(|py| -> PyResult<bool> {
            let class = match &self.adapted_class {
                Some(adapter) => adapter.bind(py).clone(),
                None => self.iterator.bind(py).get_type().into_any(),
            };
            let binding = crate::python_function::binding(py)?;
            let original_class = binding.getattr("_ASYNC_ADAPTER_TYPE")?;
            let original = binding.getattr("_ASYNC_ADAPTER_ANEXT")?;
            let code = binding.getattr("_ASYNC_ADAPTER_ANEXT_CODE")?;
            let module = binding.getattr("_ASYNC_UTILS_MODULE")?;
            let methods = class.getattr("__dict__")?;
            let method = match methods.get_item("__anext__") {
                Ok(method) => Some(method),
                Err(error) if error.is_instance_of::<pyo3::exceptions::PyKeyError>(py) => None,
                Err(error) => return Err(error),
            };
            let standard = match method {
                Some(method) if class.is(&original_class) => crate::python_function::is_standard_function(
                    &method, &original, &code, module.cast::<pyo3::types::PyModule>()?, "__anext__")?,
                _ => false,
            };
            if !standard {
                if let IteratorMode::Adapted { with_metadata } = self.mode {
                    let class = class.cast::<PyType>()?;
                    // The ordinary adapter constructor only stores its iterator.
                    let wrapper = unsafe { Bound::from_owned_ptr_or_err(py,
                        pyo3::ffi::PyType_GenericAlloc(class.as_type_ptr(), 0))? };
                    wrapper.setattr("_iterator", self.iterator.bind(py))?;
                    self.iterator = wrapper.unbind();
                    self.mode = IteratorMode::AdaptedObject { with_metadata };
                    self.adapted_class = None;
                }
                return Ok(true);
            }
            Ok(false)
        })?;
        if switched {
            let with_metadata = matches!(self.mode, IteratorMode::AdaptedObject { with_metadata: true });
            return self.next_async(with_metadata, true);
        }
        Python::attach(|py| {
            let class = match &self.adapted_class { Some(adapter) => adapter.bind(py).clone(), None => self.iterator.bind(py).get_type().into_any() };
            let method = class.getattr("__dict__")?.get_item("__anext__")?;
            let scope = crate::pyglobals::FrameGlobals::of_function(&method)?;
            let result = (|| {
                let next = crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "next"))?;
                let iterator = if matches!(self.mode, IteratorMode::AdaptedObject { .. }) {
                    self.iterator.bind(py).getattr("_iterator")?
                } else { self.iterator.bind(py).clone() };
                next.call1((iterator,))
            })();
            match result {
                Ok(value) => Ok(Some(value.unbind())),
                Err(error) => {
                    let caught = error.clone_ref(py);
                    crate::pyglobals::handle_exception(&scope, error, &["StopIteration"], || {
                        let class = crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "StopAsyncIteration"))?;
                        let wrapped = PyErr::from_value(class.call1((caught.value(py).getattr("value")?,))?);
                        wrapped.set_context(py, Some(caught.clone_ref(py)));
                        wrapped.set_cause(py, Some(caught));
                        self.async_stop = Some(wrapped);
                        Ok(None)
                    })
                }
            }
        })
    }
}

// Retain the template metadata, rather than an extra callback reference, while
// a compiled runtime import is selected or a filtered input remains deferred.
/// The iterator of a loop the live loop object iterates.
struct EmptyLoopIterator;

impl Iterator for EmptyLoopIterator {
    type Item = Value;
    fn next(&mut self) -> Option<Value> { None }
}

impl minijinja::value::LoopIterator for EmptyLoopIterator {}

enum LoopFactory {
    Imported(Value),
    Live(Py<PyAny>),
    ToIterator(Py<PyAny>),
}

impl LoopFactory {
    fn raw(&self) -> Option<&Py<PyAny>> {
        match self {
            Self::Imported(metadata) => metadata.downcast_object_ref::<CompiledTemplateMetadata>()?
                .runtime_imports.get()?.iter().find(|(name, _)| name == "auto_aiter").map(|(_, value)| value),
            Self::Live(value) | Self::ToIterator(value) => Some(value),
        }
    }
}

// The streamed filter is a Python async-generator boundary. Its input
// factory is applied again only when the generator is first advanced.
struct DeferredFilteredInput {
    source: Option<Py<PyAny>>,
    callback: Option<LoopFactory>,
    inner: Option<PythonIterator>,
    bridge: Arc<Bridge>,
    done: bool,
}

impl Iterator for DeferredFilteredInput {
    type Item = Value;

    fn next(&mut self) -> Option<Value> {
        if self.done { return None; }
        if self.inner.is_none() {
            let prepared = crate::python_async::without_reentrant_suspension(|| Python::attach(|py| -> PyResult<Py<PyAny>> {
                let source = self.source.as_ref().expect("source is retained").bind(py);
                let callback = self.callback.as_ref().and_then(LoopFactory::raw).expect("factory is retained").bind(py);
                let result = callback.call1((source,))?;
                crate::python_async::async_for_iterator(&result)
            }));
            self.callback = None;
            match prepared {
                Ok(iterator) => self.inner = Some(PythonIterator { iterator, bridge: self.bridge.clone(),
                    done: false, mode: IteratorMode::Async { with_metadata: false }, after: None,
                    async_stop: None, adapted_class: None }),
                Err(error) => {
                    self.done = true;
                    self.source = None;
                    return Some(Value::from(self.bridge.remember(error)));
                }
            }
        }
        let result = self.inner.as_mut().expect("iterator is initialized").next();
        if result.is_none() {
            self.done = true;
            self.source = None;
        }
        result
    }
}

impl minijinja::value::LoopIterator for DeferredFilteredInput {}

impl minijinja::value::LoopIterator for PythonIterator {
    fn manages_lookahead(&self) -> bool { self.managed() }

    fn lookahead_occupied(&self) -> Result<Option<bool>, Error> {
        if !self.managed() { return Ok(None); }
        Python::attach(|py| self.after_and_missing(py, "_peek_next")
            .map(|(after, missing)| Some(!after.is(&missing))))
            .map_err(|error| self.bridge.remember(error))
    }

    fn next_for_lookahead(&mut self) -> Option<Value> {
        if !self.managed() { return self.next_underlying(); }
        let cached = Python::attach(|py| -> PyResult<Option<Value>> {
            let (after, missing) = self.after_and_missing(py, "_peek_next")?;
            if after.is(&missing) { Ok(None) } else { self.bridge.value(&after).map(Some) }
        }).unwrap_or_else(|error| Some(Value::from(self.bridge.remember(error))));
        if cached.is_some() { return cached; }
        if matches!(self.mode, IteratorMode::Adapted { .. } | IteratorMode::AdaptedObject { .. } | IteratorMode::Async { .. }) {
            let raw = match self.mode {
                IteratorMode::Adapted { .. } | IteratorMode::AdaptedObject { .. } => self.adapted_next_for_lookahead(),
                IteratorMode::Async { with_metadata } => self.next_async(with_metadata, true),
                _ => unreachable!(),
            };
            let raw = match raw {
                Ok(raw) => raw,
                Err(error) => return Some(Value::from(self.bridge.remember(error))),
            };
            if raw.is_none() {
                return Python::attach(|py| -> PyResult<Option<Value>> {
                    let missing = self.after_and_missing(py, "_peek_next").map_err(|error| {
                        error.set_context(py, self.async_stop.take());
                        error
                    })?.1;
                    self.async_stop = None;
                    self.after = Some(missing.unbind());
                    Ok(None)
                }).unwrap_or_else(|error| Some(Value::from(self.bridge.remember(error))));
            }
            let raw = raw?;
            return Python::attach(|py| -> PyResult<Option<Value>> {
                let binding = crate::python_function::binding(py)?;
                let method = binding.getattr("_RUNTIME_ASYNC_LOOP_TYPE")?.getattr("_peek_next")?;
                let scope = crate::pyglobals::FrameGlobals::of_function(&method)?;
                let missing = crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "missing"))?;
                let object = raw.bind(py);
                self.after = Some(raw.clone_ref(py));
                if object.is(&missing) { Ok(None) } else { self.bridge.unpacked_value(object).map(Some) }
            }).unwrap_or_else(|error| Some(Value::from(self.bridge.remember(error))));
        }
        if self.done { return None; }
        Python::attach(|py| {
            let result = (|| -> PyResult<Option<Bound<'_, PyAny>>> {
                let binding = crate::python_function::binding(py)?;
                let method = binding.getattr("_RUNTIME_LOOP_TYPE")?.getattr("_peek_next")?;
                let scope = crate::pyglobals::FrameGlobals::of_function(&method)?;
                let callback = crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "next"))?;
                let missing = crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "missing"))?;
                let value = callback.call1((self.iterator.bind(py), &missing))?;
                self.after = Some(value.clone().unbind());
                let current_missing = crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "missing"))?;
                Ok(if value.is(&current_missing) { None } else { Some(value) })
            })();
            match result {
                Ok(Some(value)) => Some(self.bridge.value(&value).unwrap_or_else(|error| Value::from(self.bridge.remember(error)))),
                Ok(None) => None,
                Err(error) => { self.done = true; Some(Value::from(self.bridge.remember(error))) }
            }
        })
    }

    fn materialization_loop_iterator(&mut self) -> Option<Result<(Box<dyn minijinja::value::LoopIterator>, Option<Value>), Error>> {
        if matches!(self.mode, IteratorMode::SynchronousLoop) {
            // LoopContext.length collects its iterator with list(iterator),
            // which consults __iter__ and __length_hint__ before consuming it.
            return Some(Python::attach(|py| -> PyResult<(Box<dyn minijinja::value::LoopIterator>, Option<Value>)> {
                let binding = crate::python_function::binding(py)?;
                let length = binding.getattr("_RUNTIME_LOOP_TYPE")?.getattr("length")?.getattr("fget")?;
                let scope = crate::pyglobals::FrameGlobals::of_function(&length)?;
                let collect = crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "list"))?;
                let list = collect.call1((self.iterator.bind(py),))?;
                let iterator = list.try_iter()?.into_any().unbind();
                let length = crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "len"))?.call1((&list,))?;
                let length = self.bridge.value(&length)?;
                Ok((Box::new(Self { iterator, bridge: self.bridge.clone(), done: false,
                    mode: IteratorMode::SynchronousLoop, after: self.after.take(), async_stop: None, adapted_class: None }), Some(length)))
            }).map_err(|error| self.bridge.remember(error)));
        }
        if matches!(self.mode, IteratorMode::Adapted { with_metadata: true } | IteratorMode::AdaptedObject { with_metadata: true }) {
            return Some((|| -> PyResult<_> {
                let values = Python::attach(|py| PyList::empty(py).unbind());
                while let Some(value) = self.adapted_next_for_lookahead()? {
                    Python::attach(|py| values.bind(py).append(value))?;
                }
                self.async_stop = None;
                Python::attach(|py| self.finish_async_materialization(values.bind(py)))
            })().map_err(|error| self.bridge.remember(error)));
        }
        if !matches!(self.mode, IteratorMode::Async { with_metadata: true }) { return None; }
        Some((|| -> PyResult<(Box<dyn minijinja::value::LoopIterator>, Option<Value>)> {
            let values = Python::attach(|py| PyList::empty(py).unbind());
            let iterator = Python::attach(|py| crate::python_async::async_for_iterator(self.iterator.bind(py)))?;
            let mut iterator = Self { iterator, bridge: self.bridge.clone(), done: false,
                mode: IteratorMode::Async { with_metadata: false }, after: None, async_stop: None, adapted_class: None };
            while let Some(value) = iterator.next_async(false, false)? {
                Python::attach(|py| values.bind(py).append(value))?;
            }
            Python::attach(|py| self.finish_async_materialization(values.bind(py)))
        })().map_err(|error| self.bridge.remember(error)))
    }
}

impl PythonIterator {
    fn next_underlying(&mut self) -> Option<Value> {
        if self.done { return None; }
        if matches!(self.mode, IteratorMode::Adapted { .. } | IteratorMode::AdaptedObject { .. }) {
            let next = self.adapted_next_for_lookahead();
            return Python::attach(|py| match next {
                Ok(Some(value)) => Some(self.bridge.unpacked_value(value.bind(py))
                    .unwrap_or_else(|error| Value::from(self.bridge.remember(error)))),
                Ok(None) => { self.async_stop = None; None },
                Err(error) => { self.done = true; Some(Value::from(self.bridge.remember(error))) },
            });
        }
        if let IteratorMode::Async { with_metadata } = self.mode {
            let next = self.next_async(with_metadata, false);
            return Python::attach(|py| match next {
                Ok(Some(value)) => Some(self.bridge.unpacked_value(value.bind(py))
                    .unwrap_or_else(|error| Value::from(self.bridge.remember(error)))),
                Ok(None) => None,
                Err(error) => Some(Value::from(self.bridge.remember(error)))
            });
        }
        if let IteratorMode::SynchronousLoop = self.mode {
            return Python::attach(|py| {
                let next = (|| -> PyResult<Bound<'_, PyAny>> {
                    let binding = crate::python_function::binding(py)?;
                    let method = binding.getattr("_RUNTIME_LOOP_TYPE")?.getattr("__next__")?;
                    let scope = crate::pyglobals::FrameGlobals::of_function(&method)?;
                    let callback = crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "next"))?;
                    callback.call1((self.iterator.bind(py),))
                })();
                match next {
                    Ok(value) => Some(self.bridge.value(&value).unwrap_or_else(|error| Value::from(self.bridge.remember(error)))),
                    Err(error) if error.is_instance_of::<PyStopIteration>(py) => None,
                    Err(error) => { self.done = true; Some(Value::from(self.bridge.remember(error))) }
                }
            });
        }
        Python::attach(|py| match self.iterator.bind(py).call_method0("__next__") {
            Ok(value) => Some(self.bridge.value(&value).unwrap_or_else(|error| Value::from(self.bridge.remember(error)))),
            Err(error) if error.is_instance_of::<PyStopIteration>(py) => None,
            Err(error) => { self.done = true; Some(Value::from(self.bridge.remember(error))) }
        })
    }
}

impl Iterator for PythonIterator {
    type Item = Value;
    fn next(&mut self) -> Option<Value> {
        if self.managed() {
            match self.take_cached() {
                Ok(Some(value)) => return Some(Python::attach(|py| self.bridge.unpacked_value(value.bind(py)))
                    .unwrap_or_else(|error| Value::from(self.bridge.remember(error)))),
                Ok(None) => {},
                Err(error) => return Some(Value::from(self.bridge.remember(error))),
            }
        }
        self.next_underlying()
    }
}

struct PythonObject { object: Py<PyAny>, activation: Option<Py<PyAny>>, fallbacks: Option<Arc<HashMap<String, Value>>>, bridge: Arc<Bridge>, root: bool, undefined: Option<bool>, prepared_resolver: Option<Py<PyAny>>, prepared_undefined: Option<Py<PyAny>>, text: Option<String>, special: Option<Value>, dynamic_image: bool,
    // The package's special classes this value was checked against when it
    // entered the engine; None when the guard did not run for it.
    special_classes: Option<Arc<SpecialClasses>> }

struct PreparedMissingRoot { bridge: Arc<Bridge>, undefined: Py<PyAny>, name: Py<PyAny> }

impl fmt::Debug for PreparedMissingRoot {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { f.write_str("PreparedMissingRoot") }
}

impl PreparedMissingRoot {
    fn materialize(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let kwargs = PyDict::new(py);
        kwargs.set_item("name", self.name.bind(py))?;
        Ok(self.undefined.bind(py).call((), Some(&kwargs))?.unbind())
    }
}

impl Object for PreparedMissingRoot {
    fn repr(self: &Arc<Self>) -> ObjectRepr { ObjectRepr::Plain }

    fn is_true_fallible(self: &Arc<Self>) -> Result<bool, Error> {
        Python::attach(|py| self.materialize(py)?.bind(py).is_truthy())
            .map_err(|error| self.bridge.remember(error))
    }

    fn call(self: &Arc<Self>, state: &State, args: &[Value]) -> Result<Value, Error> {
        Python::attach(|py| -> PyResult<Value> {
            let callback = self.materialize(py)?;
            self.bridge.invoke(callback.bind(py), state, args, false)
        }).map_err(|error| self.bridge.remember(error))
    }
}

#[derive(Debug)]
struct PreparedFormat {
    callback: Option<Py<PyAny>>,
    has_finalizer: bool,
    needs_context: bool,
    autoescape: bool,
}
impl Object for PreparedFormat {
    fn repr(self: &Arc<Self>) -> ObjectRepr { ObjectRepr::Plain }
}

#[derive(Debug)]
struct CompiledTemplateMetadata {
    named_source: bool,
    has_finalizer: bool,
    needs_context: bool,
    kind: Py<PyAny>,
    constants: Py<PyDict>,
    auto_await: Mutex<Option<Py<PyAny>>>,
    auto_await_missing: AtomicBool,
    runtime_exports: Mutex<Option<Vec<String>>>,
    // Published once after import resolution; callbacks can borrow entries
    // without holding a lock or adding an observable Python reference.
    runtime_imports: OnceLock<Vec<(String, Py<PyAny>)>>,
    runtime_builtins: Mutex<Option<Py<PyDict>>>,
    runtime_builtins_prepared: bool,
    runtime_from_code: Mutex<Option<Py<PyAny>>>,
}

struct TemplateSelection {
    builtins: Option<Py<PyDict>>,
    method: Py<PyAny>,
}
impl Object for CompiledTemplateMetadata {
    fn repr(self: &Arc<Self>) -> ObjectRepr { ObjectRepr::Plain }
}

fn ensure_auto_await_available(bridge: &Arc<Bridge>, state: &State) -> Result<(), Error> {
    let missing = state.template_metadata()
        .and_then(|metadata| metadata.downcast_object_ref::<CompiledTemplateMetadata>())
        .is_some_and(|metadata| metadata.auto_await_missing.load(Ordering::Relaxed));
    if !missing { return Ok(()); }
    Err(bridge.remember(Python::attach(|py| {
        let kwargs = PyDict::new(py);
        let result = (|| -> PyResult<PyErr> {
            kwargs.set_item("name", "auto_await")?;
            let exception = py.get_type::<pyo3::exceptions::PyNameError>()
                .call(("name 'auto_await' is not defined",), Some(&kwargs))?;
            Ok(PyErr::from_value(exception))
        })();
        match result { Ok(error) | Err(error) => error }
    })))
}

fn deferred_call<'t, 'e>(bridge: &Arc<Bridge>, state: &mut State<'t, 'e>,
    execute: &mut dyn FnMut(&mut State<'t, 'e>) -> Result<Value, Error>) -> Result<Value, Error> {
    let metadata = state.template_metadata().cloned();
    let frames = state.render_frames();
    let filtered_loop_depth = state.filtered_loop_depth();
    let selected = Python::attach(|py| metadata.as_ref()
        .and_then(|metadata| metadata.downcast_object_ref::<CompiledTemplateMetadata>())
        .and_then(|metadata| metadata.auto_await.lock().unwrap().as_ref().map(|value| value.clone_ref(py))));
    // SAFETY: every bridge environment is an Environment<'static> (configure).
    // Awaited after this render, or from another task, the body runs on this copy.
    let detached = unsafe { state.detach_deferred() }
        .map(|activation| crate::python_deferred::Detached { activation, bridge: bridge.clone() });
    let state = std::cell::RefCell::new(state);
    let mut body = || -> PyResult<Py<PyAny>> {
        let mut state = state.try_borrow_mut().map_err(|_| PyRuntimeError::new_err("render state is already active"))?;
        let value = execute(&mut state).map_err(|error| bridge.take_error().unwrap_or_else(|| crate::error(error)))?;
        Python::attach(|py| bridge.python(py, &value))
    };
    let result = crate::python_deferred::with_body(&mut body, |token| {
        let coroutine = Python::attach(|py| crate::python_deferred::coroutine(py, token, detached))?;
        crate::python_async::with_render_frames(frames, filtered_loop_depth, || crate::python_macro::with_state_cell(
            bridge, &state, || crate::python_async::auto_await(
                coroutine, selected, bridge.await_adapter_probe.load(Ordering::Relaxed))))
    }).map_err(|error| bridge.remember(error))?;
    // After the await, possibly on another thread (see fiber_local).
    crate::fiber_local::attach(move |py| bridge.value(result.bind(py))).map_err(|error| bridge.remember(error))
}

fn runtime_export_names(py: Python<'_>, binding: &Bound<'_, PyAny>, is_async: bool) -> PyResult<Vec<String>> {
    let compiler = crate::find_loaded_module(py, pyo3::intern!(py, "jinja2.compiler"))?
        .ok_or_else(|| unsupported(py, "Jinja compiler module is unavailable for runtime imports"))?;
    let compiler = compiler.bind(py).cast::<PyModule>().map_err(|_| unsupported(py,
        "Jinja compiler module is unavailable for runtime imports"))?;
    let import = binding.getattr("_load_native_compiler_runtime")?;
    // Jinja's compiler resolves async_exported before exported, then adds
    // exported + async_exported and sorts the resulting names.
    let asynchronous_module = import.call1((compiler.dict(), "async_exported"))?;
    let asynchronous = runtime_import_attribute(&asynchronous_module, "async_exported")?;
    let exported_module = import.call1((compiler.dict(), "exported"))?;
    let exported = runtime_import_attribute(&exported_module, "exported")?;
    let combined = if is_async { unsafe { Bound::from_owned_ptr_or_err(py,
        pyo3::ffi::PyNumber_Add(exported.as_ptr(), asynchronous.as_ptr()))? } } else { exported };
    let mut names = Vec::new();
    for value in combined.try_iter()? {
        let value = value?;
        if !value.is_exact_instance_of::<PyString>() {
            return Err(unsupported(py, "custom runtime export names need a native compiler adapter"));
        }
        let name: String = value.extract().map_err(|_| unsupported(py,
            "runtime export names with non-scalar Unicode need a native compiler adapter"))?;
        let mut chars = name.bytes();
        if !chars.next().is_some_and(|c| c.is_ascii_alphabetic() || c == b'_')
            || !chars.all(|c| c.is_ascii_alphanumeric() || c == b'_')
            || matches!(name.as_str(), "False" | "None" | "True" | "and" | "as" | "assert" | "async" | "await"
                | "break" | "class" | "continue" | "def" | "del" | "elif" | "else" | "except" | "finally"
                | "for" | "from" | "global" | "if" | "import" | "in" | "is" | "lambda" | "nonlocal" | "not"
                | "or" | "pass" | "raise" | "return" | "try" | "while" | "with" | "yield") {
            return Err(unsupported(py, "runtime export name requires a native import syntax adapter"));
        }
        names.push(name);
    }
    if names.is_empty() { return Err(unsupported(py, "empty runtime export list requires a native import syntax adapter")); }
    names.sort();
    Ok(names)
}

fn template_builtins(py: Python<'_>, bridge: &Bridge) -> PyResult<TemplateSelection> {
    unsafe extern "C" { static PyMethod_Type: pyo3::ffi::PyTypeObject; }
    let template_class = bridge.config.bind(py).getattr("environment")?.getattr("template_class")?;
    let method = template_class.getattr("from_code")?;
    if unsafe { pyo3::ffi::Py_TYPE(method.as_ptr()) as *const _ != std::ptr::addr_of!(PyMethod_Type) } {
        return Ok(TemplateSelection { builtins: None, method: method.unbind() });
    }
    let function = method.getattr("__func__")?;
    let builtins = if function.is_exact_instance_of::<PyFunction>() {
        Some(function.getattr("__builtins__")?.cast_into::<PyDict>()?.unbind())
    } else { None };
    Ok(TemplateSelection { builtins, method: method.unbind() })
}

fn runtime_import_attribute<'py>(module: &Bound<'py, PyAny>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    let py = module.py();
    match module.getattr(name) {
        Ok(value) => Ok(value),
        Err(error) if error.is_instance_of::<PyAttributeError>(py) => {
            let module_name = match module.getattr("__name__") {
                Ok(name) if name.is_instance_of::<PyString>() => Some(name),
                _ => None,
            };
            // Import-from consults the interpreter's module registry directly;
            // importing sys here would add an unrelated observable import.
            let imported_name = PyString::new(py, name);
            if let Some(module_name) = &module_name {
                let full = unsafe { Bound::from_owned_ptr_or_err(py, pyo3::ffi::PyUnicode_FromFormat(
                    c"%U.%U".as_ptr(), module_name.as_ptr(), imported_name.as_ptr()))? };
                let value = unsafe { pyo3::ffi::PyImport_GetModule(full.as_ptr()) };
                if !value.is_null() { return Ok(unsafe { Bound::from_owned_ptr(py, value) }); }
                if unsafe { !pyo3::ffi::PyErr_Occurred().is_null() } { return Err(PyErr::fetch(py)); }
            }
            // IMPORT_FROM obtains the filename from a module's dictionary via
            // the CPython module API, not through arbitrary __file__ lookup.
            // Non-module proxies and modules without a string filename have
            // an unknown location and the API's error is discarded.
            let path = unsafe { Bound::from_owned_ptr_or_err(py,
                pyo3::ffi::PyModule_GetFilenameObject(module.as_ptr())) }.ok();
            let initializing = path.is_some() && module.getattr("__spec__").ok()
                .and_then(|spec| spec.getattr("_initializing").ok())
                .and_then(|value| value.is_truthy().ok()).unwrap_or(false);
            let unknown = PyString::new(py, "<unknown module name>");
            let label = module_name.as_ref().unwrap_or(unknown.as_any());
            let message = unsafe { Bound::from_owned_ptr_or_err(py, match &path {
                Some(path) if initializing => pyo3::ffi::PyUnicode_FromFormat(
                    c"cannot import name %R from partially initialized module %R (most likely due to a circular import) (%S)".as_ptr(),
                    imported_name.as_ptr(), label.as_ptr(), path.as_ptr()),
                Some(path) => pyo3::ffi::PyUnicode_FromFormat(c"cannot import name %R from %R (%S)".as_ptr(),
                    imported_name.as_ptr(), label.as_ptr(), path.as_ptr()),
                None => pyo3::ffi::PyUnicode_FromFormat(c"cannot import name %R from %R (unknown location)".as_ptr(),
                    imported_name.as_ptr(), label.as_ptr()),
            })? };
            let kwargs = PyDict::new(py);
            if let Some(module_name) = module_name { kwargs.set_item("name", module_name)?; }
            if let Some(path) = path { kwargs.set_item("path", path)?; }
            Err(PyErr::from_value(py.get_type::<pyo3::exceptions::PyImportError>().call((message,), Some(&kwargs))?))
        },
        Err(error) => Err(error),
    }
}

#[pyclass]
struct RuntimeImportResolver { module: Option<Py<PyAny>> }

#[pymethods]
impl RuntimeImportResolver {
    fn __call__<'py>(&mut self, py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
        let module = self.module.as_ref().ok_or_else(||
            pyo3::exceptions::PyRuntimeError::new_err("runtime import resolver already failed"))?;
        let result = runtime_import_attribute(module.bind(py), name);
        if result.is_err() {
            // A traceback may retain this resolver. Do not let that retain an
            // otherwise released import object after a failed lookup.
            let module = self.module.take();
            drop(module);
        }
        result
    }
}

pub(crate) fn escape_text(text: &str) -> Cow<'_, str> {
    if !text.bytes().any(|ch| matches!(ch, b'&' | b'<' | b'>' | b'\'' | b'"')) { return Cow::Borrowed(text); }
    let mut result = String::with_capacity(text.len());
    for ch in text.chars() {
        match ch {
            '&' => result.push_str("&amp;"), '<' => result.push_str("&lt;"), '>' => result.push_str("&gt;"),
            '\'' => result.push_str("&#39;"), '"' => result.push_str("&#34;"), _ => result.push(ch),
        }
    }
    Cow::Owned(result)
}

// Deferred markers are currently embedded in the rendered XML as text.
// Reject a marker originating in a Python value instead of letting user data
// impersonate a package insertion. Actual specials are native VM values.
fn guard_deferred_marker(bridge: &Bridge, text: &Bound<'_, PyAny>, capturing: bool) -> PyResult<bool> {
    if !bridge.guard_reserved_literals { return Ok(false); }
    // SAFETY: text is the Unicode result of Python's output conversion.
    let control = unsafe { pyo3::ffi::PyUnicode_FindChar(text.as_ptr(), 1, 0, isize::MAX, 1) };
    if control == -2 { return Err(PyErr::fetch(text.py())); }
    if control < 0 { return Ok(false); }
    if bridge.specials.lock().unwrap().is_empty() { return Ok(false); }
    if capturing { return Ok(true); }
    Err(unsupported(text.py(), "user text may contain a deferred marker and requires a native fragment adapter"))
}

fn guard_fragment_output(bridge: &Bridge, text: &Bound<'_, PyAny>, output: &minijinja::Output<'_>) -> PyResult<bool> {
    // The document renderer routes this root stream through the typed
    // fragment converter and materializer. Nested captures and all text-only
    // render paths continue to enforce the existing guard.
    if output.is_unjoined_fragment_root() && !output.root_has_restricted_capture() {
        return Ok(false);
    }
    guard_deferred_marker(bridge, text, output.is_capturing())
}

fn dispatch_replaced_block(bridge: &Arc<Bridge>, state: &State, name: &str, depth: usize,
    output: &mut minijinja::Output<'_>) -> Result<bool, Error> {
    let iterator = Python::attach(|py| crate::python_macro::with_state(bridge, state, || -> PyResult<Option<Py<PyAny>>> {
        let context = bridge.python(py, state.runtime_context())?;
        let blocks = context.bind(py).getattr("blocks")?;
        let stack = if blocks.is_exact_instance_of::<PyDict>() {
            match blocks.cast::<PyDict>()?.get_item(name)? {
                Some(stack) => stack,
                // In a registry the binding populated, a missing entry was removed.
                None if crate::python_blocks::is_registry(py, context.bind(py))? =>
                    return Err(pyo3::exceptions::PyKeyError::new_err(name.to_owned())),
                None => return Ok(None),
            }
        } else {
            blocks.get_item(name)?
        };
        let callable = stack.get_item(depth)?;
        if crate::python_blocks::is_native(py, &callable, name, depth)? { return Ok(None); }
        let body = callable.call1((context.bind(py),))?;
        let iterator = if bridge.asynchronous {
            crate::python_async::async_for_iterator(&body)?
        } else {
            body.try_iter()?.into_any().unbind()
        };
        Ok(Some(iterator))
    })).map_err(|error| bridge.remember(error))?;
    let Some(iterator) = iterator else { return Ok(false); };
    if !output.supports_fragments() {
        return Err(Error::new(ErrorKind::InvalidOperation, "foreign block output requires fragment capture"));
    }
    loop {
        let next = crate::python_macro::with_state(bridge, state, || -> PyResult<Option<Py<PyAny>>> {
            if bridge.asynchronous {
                let awaitable = Python::attach(|py| -> PyResult<Py<PyAny>> {
                    let iterator = iterator.bind(py);
                    let slot = crate::python_async::async_slot(iterator, pyo3::ffi::Py_am_anext)
                        .ok_or_else(|| PyTypeError::new_err("async block iterator has no __anext__"))?;
                    let value = unsafe { Bound::from_owned_ptr_or_err(py, slot(iterator.as_ptr()))? };
                    crate::python_async::await_iterator(&value)
                })?;
                match crate::python_async::drive(awaitable) {
                    Ok(value) => Ok(Some(value)),
                    // After the await, possibly on another thread (see fiber_local).
                    Err(error) => crate::fiber_local::attach(move |py|
                        if error.is_instance_of::<pyo3::exceptions::PyStopAsyncIteration>(py) { Ok(None) } else { Err(error) }),
                }
            } else {
                Python::attach(|py| {
                    let value = unsafe { pyo3::ffi::PyIter_Next(iterator.bind(py).as_ptr()) };
                    if value.is_null() {
                        if unsafe { pyo3::ffi::PyErr_Occurred().is_null() } { Ok(None) }
                        else { Err(PyErr::fetch(py)) }
                    } else { Ok(Some(unsafe { Bound::from_owned_ptr(py, value) }.unbind())) }
                })
            }
        }).map_err(|error| bridge.remember(error))?;
        let Some(value) = next else { break; };
        let (fragment, marked) = Python::attach(|py| -> PyResult<(Value, bool)> {
            let value = value.bind(py);
            let marked = if value.is_instance_of::<PyString>() { guard_fragment_output(bridge, value, output)? } else { false };
            Ok((bridge.opaque_value(value), marked))
        }).map_err(|error| bridge.remember(error))?;
        if marked { output.mark_untrusted_control(); }
        output.write_fragment(fragment)?;
    }
    Ok(true)
}

fn write_root_deferred(bridge: &Arc<Bridge>, output: &mut minijinja::Output<'_>, special: &Value) -> Result<bool, Error> {
    if !output.supports_fragments() || output.is_capturing() { return Ok(false); }
    let Some(index) = special.string_provenance() else { return Ok(false); };
    let Some(materialized) = docxtpl_core::materialize_current_deferred(index as usize) else { return Ok(false); };
    let text = materialized.map_err(|message| Error::new(ErrorKind::InvalidOperation, message))?;
    let value = Python::attach(|py| bridge.opaque_value(PyString::new(py, &text).as_any()));
    output.write_fragment(value)?;
    Ok(true)
}

/// Complete a loop call in Python on the live `LoopContext`, through the
/// template's call dispatch, when that dispatch is not the standard one.
fn python_loop_call(bridge: &Arc<Bridge>, py: Python<'_>, state: &State, called: &Value, args: &[Value])
    -> PyResult<Value> {
    let source = called.downcast_object::<minijinja::RuntimeLoop>()
        .ok_or_else(|| unsupported(py, "loop call without a loop"))?;
    let object = crate::python_loop::live_loop(py, bridge, &source)?
        .ok_or_else(|| unsupported(py, "custom context calls for native loop objects need a runtime adapter"))?;
    let value = bridge.invoke(object.bind(py), state, args, false)?;
    Ok(Value::from_object(minijinja::LoopCallOutcome(value)))
}

/// Whether `getitem`'s module globals used for a string key on a loop are the
/// builtins the native lookup reproduces.
/// The environment's delimiters and line prefixes as MiniJinja's syntax.
fn syntax_config(environment: &Bound<'_, PyAny>) -> PyResult<minijinja::syntax::SyntaxConfig> {
    let text = |name: &str| -> PyResult<String> { environment.getattr(name)?.extract() };
    let prefix = |name: &str| -> PyResult<Option<String>> { environment.getattr(name)?.extract() };
    let mut builder = minijinja::syntax::SyntaxConfig::builder();
    builder.block_delimiters(text("block_start_string")?, text("block_end_string")?)
        .variable_delimiters(text("variable_start_string")?, text("variable_end_string")?)
        .comment_delimiters(text("comment_start_string")?, text("comment_end_string")?);
    if let Some(prefix) = prefix("line_statement_prefix")? { builder.line_statement_prefix(prefix); }
    if let Some(prefix) = prefix("line_comment_prefix")? { builder.line_comment_prefix(prefix); }
    builder.build().map_err(|error| crate::python_bridge::unsupported(environment.py(), &format!("Jinja2 syntax: {error}")))
}

fn standard_loop_item_globals(bridge: &Arc<Bridge>, dispatcher: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = dispatcher.py();
    let Ok(function) = dispatcher.getattr("__func__") else { return Ok(false); };
    let scope = crate::pyglobals::FrameGlobals::of_function(&function)?;
    let binding = bridge.lookup_binding.bind(py);
    for (name, original) in [("isinstance", "_BUILTIN_ISINSTANCE"), ("str", "_BUILTIN_STR"),
                             ("getattr", "_BUILTIN_GETATTR")] {
        if !crate::pyglobals::frame_global(&scope, &PyString::new(py, name))?.is(&binding.getattr(original)?) {
            return Ok(false);
        }
    }
    Ok(true)
}

/// A loop lookup whose policy or lookup function is not the standard one runs
/// in Python on the live `LoopContext`. Returns `None` for the native path, or
/// the object to pass to the custom lookup; a loop that cannot be handed over
/// (a filtered loop) keeps the refusal.
fn custom_loop_lookup(bridge: &Arc<Bridge>, py: Python<'_>, standard: PyResult<()>, target: &Value)
    -> PyResult<Option<Py<PyAny>>> {
    let refusal = match standard {
        Ok(()) => return Ok(None),
        Err(error) => error,
    };
    let unsupported = py.import("docxtpl_native")?.getattr("UnsupportedFeature")?;
    if !refusal.is_instance(py, &unsupported) { return Err(refusal); }
    let Some(source) = target.downcast_object::<minijinja::RuntimeLoop>() else { return Err(refusal); };
    match crate::python_loop::live_loop(py, bridge, &source)? {
        Some(object) => Ok(Some(object)),
        None => Err(refusal),
    }
}

fn format_python_value(bridge: &Arc<Bridge>, output: &mut minijinja::Output<'_>, state: &State,
    value: &Value, prepared: Option<&Value>) -> Result<(), Error> {
    if let Some(module) = value.downcast_object::<minijinja::RuntimeModule>() {
        let has_finalizer = prepared.and_then(|value| value.downcast_object_ref::<PreparedFormat>())
            .map(|value| value.has_finalizer)
            .or_else(|| state.template_metadata().and_then(|value| value.downcast_object_ref::<CompiledTemplateMetadata>())
                .map(|value| value.has_finalizer))
            .unwrap_or(true);
        if !has_finalizer {
            let text = Python::attach(|py| crate::python_module::native_text(py, &module, bridge))
                .map_err(|error| bridge.remember(error))?;
            if let Some(text) = text { return format_python_value(bridge, output, state, &text, prepared); }
        }
    }
    let prepared = prepared.and_then(|value| value.downcast_object_ref::<PreparedFormat>());
    let compiled = state.template_metadata().and_then(|value| value.downcast_object_ref::<CompiledTemplateMetadata>());
    let plan = prepared.map(|value| (value.has_finalizer, value.needs_context))
        .or_else(|| compiled.map(|value| (value.has_finalizer, value.needs_context)))
        .or_else(|| bridge.finalizer_plans.lock().unwrap().get(state.name()).copied());
    let autoescape = prepared.map(|value| value.autoescape).unwrap_or(state.auto_escape() != AutoEscape::None);
    if plan == Some((false, false)) {
        if let Some(special) = value.downcast_object_ref::<PythonObject>().and_then(|object| {
            let identity = Python::attach(|py| object.object.bind(py).as_ptr() as usize);
            (!bridge.lazy_images.lock().unwrap().contains(&identity)).then_some(object.special.as_ref()).flatten()
        }) {
            let object = value.downcast_object_ref::<PythonObject>().expect("special is a Python object");
            let live = Python::attach(|py| crate::inline_image::live_registered_image(object.object.bind(py), autoescape)
                .map(|text| text.map(|text| bridge.opaque_value(&text))))
                .map_err(|error| bridge.remember(error))?;
            if let Some(value) = live {
                let marked = Python::attach(|py| {
                    let text = bridge.python(py, &value)?;
                    guard_deferred_marker(bridge, text.bind(py), output.is_capturing())
                }).map_err(|error| bridge.remember(error))?;
                if marked { output.mark_restricted_control(); }
                if output.supports_fragments() { return output.write_fragment(value); }
                let text = Python::attach(|py| bridge.python(py, &value)?.bind(py).extract::<String>())
                    .map_err(|error| bridge.remember(error))?;
                return output.write_str(&text).map_err(|_| Error::new(ErrorKind::WriteFailure, "failed writing image"));
            }
            if output.is_capturing() {
                let identity = Python::attach(|py| object.object.bind(py).as_ptr() as usize);
                if let Some(materialized) = docxtpl_core::materialize_current_registered_image(identity) {
                    let text = materialized.map_err(|message| Error::new(ErrorKind::InvalidOperation, message))?;
                    let marked = Python::attach(|py| guard_deferred_marker(bridge, PyString::new(py, &text).as_any(), true))
                        .map_err(|error| bridge.remember(error))?;
                    if marked { output.mark_restricted_control(); }
                    if output.supports_fragments() {
                        let value = Python::attach(|py| bridge.opaque_value(PyString::new(py, &text).as_any()));
                        return output.write_fragment(value);
                    }
                    return output.write_str(&text).map_err(|_| Error::new(ErrorKind::WriteFailure, "failed writing captured image"));
                }
            }
            if write_root_deferred(bridge, output, special)? { return Ok(()); }
            if output.supports_fragments() && !output.is_capturing() && special.string_provenance().is_some() {
                return output.write_fragment(special.clone());
            }
            if output.is_capturing() { output.mark_restricted_control(); }
            return output.write_str(special.as_str().ok_or_else(|| Error::new(
                ErrorKind::InvalidOperation, "registered deferred value is not text"))?)
                .map_err(|_| Error::new(ErrorKind::WriteFailure, "failed writing deferred value"));
        }
        if let Some((object, text)) = value.downcast_object_ref::<PythonObject>()
            .and_then(|object| object.text.as_deref().map(|text| (object, text))) {
            if text.as_bytes().contains(&1) {
                let marked = Python::attach(|py| guard_fragment_output(bridge, object.object.bind(py), output))
                    .map_err(|error| bridge.remember(error))?;
                if marked { output.mark_untrusted_control(); }
            }
            bridge.fast_formats.fetch_add(1, Ordering::Relaxed);
            let text = if autoescape { escape_text(text) } else { Cow::Borrowed(text) };
            if output.supports_fragments() {
                let fragment = Python::attach(|py| {
                    if autoescape {
                        bridge.opaque_value(pyo3::types::PyString::new(py, &text).as_any())
                    } else {
                        bridge.opaque_value(object.object.bind(py))
                    }
                });
                return output.write_fragment(fragment);
            }
            return output.write_str(&text).map_err(|_| Error::new(ErrorKind::WriteFailure, "failed writing native string"));
        }
    }
    let dynamic_input = value.downcast_object_ref::<PythonObject>().is_some_and(|object| object.dynamic_image);
    let capturing = output.is_capturing();
    let (text, special, image_text) = Python::attach(|py| -> PyResult<(Option<Py<PyAny>>, Option<Value>, bool)> {
        bridge.formats.fetch_add(1, Ordering::Relaxed);
        let value = bridge.python(py, value)?;
        let context = bridge.python(py, state.runtime_context())?;
        let callback = prepared.and_then(|value| value.callback.as_ref())
            .map(|callback| callback.clone_ref(py)).unwrap_or_else(|| py.None());
        crate::python_macro::with_state(bridge, state, || {
            let finalized = crate::python_format::finalize_value(py, bridge.config.bind(py), value.bind(py),
                py.None().bind(py), autoescape, Some(state.name()), Some(callback.bind(py)), prepared.is_some(), Some(context.bind(py)),
                compiled.map(|plan| (plan.has_finalizer, plan.kind.bind(py))))?;
            let identity = finalized.as_ptr() as usize;
            let lazy = bridge.lazy_images.lock().unwrap().contains(&identity)
                || (dynamic_input && value.bind(py).is(&finalized))
                || (plan.is_some_and(|(has_finalizer, _)| has_finalizer)
                    && bridge.guard_unmaterialized_specials
                    && docxtpl_core::has_owned_callback_image_scope()
                    && concrete_default_image(&finalized)?);
            let special = if lazy { None } else { bridge.specials.lock().unwrap().get(&identity).cloned() };
            if special.is_some() {
                if let Some(text) = crate::inline_image::live_registered_image(&finalized, autoescape)? {
                    return Ok((Some(text.unbind()), None, true));
                }
            }
            if special.is_some() && capturing {
                if let Some(materialized) = docxtpl_core::materialize_current_registered_image(identity) {
                    return Ok((Some(PyString::new(py, &materialized.map_err(crate::error)?).into_any().unbind()), None, true));
                }
            }
            if special.is_some() { return Ok((None, special, false)); }
            if lazy && default_image_protocol(&finalized, autoescape)? {
                return Ok((Some(crate::inline_image::image_insert(&finalized)?.unbind()), None, true));
            }
            Ok((Some(crate::python_format::stringify_value(&finalized, autoescape)?.unbind()), None, false))
        })
    }).map_err(|error| bridge.remember(error))?;
    if let Some(special) = special {
        if write_root_deferred(bridge, output, &special)? { return Ok(()); }
        if output.supports_fragments() && !output.is_capturing() && special.string_provenance().is_some() {
            return output.write_fragment(special);
        }
        if output.is_capturing() { output.mark_restricted_control(); }
        return output.write_str(special.as_str().ok_or_else(|| Error::new(
            ErrorKind::InvalidOperation, "registered deferred value is not text"))?)
            .map_err(|_| Error::new(ErrorKind::WriteFailure, "failed writing deferred value"));
    }
    let text = text.expect("formatting returns text or a deferred value");
    if value.downcast_object_ref::<PythonObject>().is_some() || value.kind() != ValueKind::String
        || plan.is_some_and(|(has_finalizer, _)| has_finalizer) {
        let marked = Python::attach(|py| if image_text {
            guard_deferred_marker(bridge, text.bind(py), output.is_capturing())
        } else { guard_fragment_output(bridge, text.bind(py), output) })
            .map_err(|error| bridge.remember(error))?;
        if marked {
            if image_text { output.mark_restricted_control(); }
            else { output.mark_untrusted_control(); }
        }
    }
    if output.supports_fragments() {
        let value = Python::attach(|py| bridge.opaque_value(text.bind(py)));
        return output.write_fragment(value);
    }
    let text = Python::attach(|py| text.bind(py).extract::<String>())
        .map_err(|error| bridge.remember(error))?;
    output.write_str(&text).map_err(|_| Error::new(ErrorKind::WriteFailure, "failed writing formatted Python value"))
}

pub(crate) fn concrete_default_image(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    let Some(image) = IMAGE_TYPE.get(value.py()) else { return Ok(false); };
    if unsafe { pyo3::ffi::PyType_IsSubtype(value.get_type().as_type_ptr(), image.bind(value.py()).as_type_ptr()) } == 0 {
        return Ok(false);
    }
    let py = value.py();
    let package = match crate::find_loaded_module(py, pyo3::intern!(py, "docxtpl_native"))? {
        Some(package) if !package.bind(py).is_none() => package.into_bound(py),
        _ => py.import("docxtpl_native")?.into_any(),
    };
    let image = package.getattr("InlineImage")?;
    let subdoc = package.getattr("Subdoc")?;
    let Some(classes) = crate::live_routing::default_special_types(package.as_any(), &image, &subdoc)? else {
        return Ok(false);
    };
    Ok(unsafe { pyo3::ffi::PyType_IsSubtype(value.get_type().as_type_ptr(), classes.0.as_type_ptr()) != 0 })
}

/// Whether a package image still uses the package's own output protocols, so the
/// extension can insert it without calling them.
pub(crate) fn default_image_protocol(value: &Bound<'_, PyAny>, autoescape: bool) -> PyResult<bool> {
    let py = value.py();
    let package = match crate::find_loaded_module(py, pyo3::intern!(py, "docxtpl_native"))? {
        Some(package) if !package.bind(py).is_none() => package.into_bound(py),
        _ => py.import("docxtpl_native")?.into_any(),
    };
    let class = value.get_type();
    // A custom metaclass or attribute hook can change the delegated lookup;
    // let Python perform those user protocols.
    let object = py.get_type::<PyString>().getattr("__base__")?;
    if !class.get_type().is(&py.get_type::<PyType>())
        || !class.getattr("__getattribute__")?.is(&object.getattr("__getattribute__")?) {
        return Ok(false);
    }
    let protocol = if autoescape { "__html__" } else { "__str__" };
    let original = if autoescape { "_OriginalNativeImageHtml" } else { "_OriginalNativeImageStr" };
    let code = if autoescape { "_OriginalNativeImageHtmlCode" } else { "_OriginalNativeImageStrCode" };
    let method = class.getattr(protocol)?;
    let insert = class.getattr("_insert_image")?;
    if !method.is(&package.getattr(original)?) || !method.getattr("__code__")?.is(&package.getattr(code)?)
        || !insert.is(&package.getattr("_OriginalNativeImageInsert")?)
        || !insert.getattr("__code__")?.is(&package.getattr("_OriginalNativeImageInsertCode")?) {
        return Ok(false);
    }
    // HTML lookup is instance-sensitive, as is the delegated _insert_image.
    let dictionary = unsafe { pyo3::ffi::PyObject_GenericGetDict(value.as_ptr(), std::ptr::null_mut()) };
    let dictionary = unsafe { Bound::from_owned_ptr_or_err(py, dictionary) }?.cast_into::<PyDict>()?;
    Ok(!dictionary.contains("_insert_image")? && (!autoescape || !dictionary.contains("__html__")?))
}

impl PythonObject {
    fn lookup(self: &Arc<Self>, key: &Value, attribute_first: bool) -> Option<Value> {
        self.lookup_with_prepared_missing(key, attribute_first, false)
    }

    fn lookup_with_prepared_missing(self: &Arc<Self>, key: &Value, attribute_first: bool, prepared: bool) -> Option<Value> {
        match Python::attach(|py| -> PyResult<Option<Value>> {
            let object = self.object.bind(py);
            let key_py = self.bridge.python(py, key)?;
            if let Some(context) = &self.activation {
                let resolver = self.prepared_resolver.as_ref().expect("activation has resolver snapshot");
                let (value, standard) = crate::python_context_helpers::resolve(py, context.bind(py), resolver.bind(py), key_py.bind(py))?;
                if let Some(value) = value {
                    return self.bridge.value(&value).map(Some);
                }
                if standard {
                    if let Some(value) = key.as_str().and_then(|name| self.fallbacks.as_ref().and_then(|values| values.get(name))) {
                        return Ok(Some(value.clone()));
                    }
                }
                if key.as_str().is_some_and(|name| self.bridge.globals.lock().unwrap().contains(name)) { return Ok(None); }
                if prepared {
                    return Ok(Some(Value::from_object(PreparedMissingRoot {
                        bridge: self.bridge.clone(), undefined: self.prepared_undefined.as_ref().expect("activation has undefined snapshot").clone_ref(py), name: key_py,
                    })));
                }
                let missing = self.bridge.config.bind(py).call_method1("missing", (object, key_py, true))?;
                return self.bridge.value(&missing).map(Some);
            }
            let environment = self.bridge.config.bind(py).getattr("environment")?;
            let value = crate::python_lookup::lookup_with_binding(&environment, object, key_py.bind(py), attribute_first, self.bridge.lookup_binding.bind(py))?;
            self.bridge.value(&value).map(Some)
        }) {
            Ok(value) => value,
            Err(error) => Some(Value::from(self.bridge.remember(error))),
        }
    }
}

impl fmt::Debug for PythonObject {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { f.write_str("PythonObject") }
}

fn async_loop_iterator(bridge: &Arc<Bridge>, object: &Py<PyAny>, with_metadata: bool,
    selected: Option<&Py<PyAny>>) -> Option<Result<Box<dyn minijinja::value::LoopIterator>, Error>> {
        let iterator = Python::attach(|py| -> PyResult<(Py<PyAny>, IteratorMode, Option<Py<PyAny>>)> {
            let object = object.bind(py);
            if !object.hasattr("__aiter__")? {
                let class = PythonIterator::adapted_type(py, selected.map(|function| function.bind(py)))?;
                let iterator = object.try_iter()?.into_any();
                if !PythonIterator::adapter_constructor_is_standard(class.bind(py))? {
                    let wrapper = class.bind(py).call1((iterator,))?;
                    let same_class = wrapper.get_type().is(class.bind(py));
                    if with_metadata {
                        let mode = if same_class { IteratorMode::AdaptedObject { with_metadata } }
                            else { IteratorMode::Async { with_metadata } };
                        return Ok((wrapper.unbind(), mode, None));
                    }
                    let result = crate::python_async::async_for_iterator(&wrapper)?;
                    let mode = if same_class && result.bind(py).is(&wrapper) { IteratorMode::AdaptedObject { with_metadata } }
                        else { IteratorMode::Async { with_metadata } };
                    return Ok((result, mode, None));
                }
                if !with_metadata && !PythonIterator::adapter_aiter_is_standard(class.bind(py))? {
                    let adapter_type = class.bind(py).cast::<PyType>()?;
                    let wrapper = unsafe { Bound::from_owned_ptr_or_err(py,
                        pyo3::ffi::PyType_GenericAlloc(adapter_type.as_type_ptr(), 0))? };
                    wrapper.setattr("_iterator", iterator)?;
                    let result = crate::python_async::async_for_iterator(&wrapper)?;
                    let mode = if result.bind(py).is(&wrapper) { IteratorMode::AdaptedObject { with_metadata } }
                        else { IteratorMode::Async { with_metadata } };
                    return Ok((result, mode, None));
                }
                return Ok((iterator.unbind(), IteratorMode::Adapted { with_metadata }, Some(class)));
            }
            let first = object.call_method0("__aiter__")?;
            if with_metadata { return Ok((first.unbind(), IteratorMode::Async { with_metadata }, None)); }
            // Async-for obtains its iterator through the type slot; a metadata
            // loop instead retains auto_aiter's result and calls __anext__.
            Ok((crate::python_async::async_for_iterator(&first)?, IteratorMode::Async { with_metadata }, None))
        });
        Some(iterator.map(|(iterator, mode, adapted_class)| Box::new(PythonIterator {
            iterator, bridge: bridge.clone(), done: false, mode, after: None, async_stop: None, adapted_class }) as Box<dyn minijinja::value::LoopIterator>)
            .map_err(|error| bridge.remember(error)))
}

/// Whether instances of `class` define `__iter__`: its `tp_iter` slot, as
/// `iter()` uses. Reading the slot runs no Python code; a lookup on the class
/// would run a metaclass `__getattr__` that Jinja never calls, and build an
/// AttributeError for every plain object the VM classifies.
fn type_defines_iteration(class: &Bound<'_, PyType>) -> bool {
    !unsafe { pyo3::ffi::PyType_GetSlot(class.as_type_ptr(), pyo3::ffi::Py_tp_iter) }.is_null()
}

impl Object for PythonObject {
    fn template_name(self: &Arc<Self>) -> Result<Option<String>, Error> {
        Python::attach(|py| {
            match self.object.bind(py).cast::<PyString>() {
                Ok(text) => text.to_str().map(|text| Some(text.to_owned()))
                    .map_err(|error| self.bridge.remember(error)),
                Err(_) => Ok(None),
            }
        })
    }

    fn repr(self: &Arc<Self>) -> ObjectRepr {
        Python::attach(|py| {
            let object = self.object.bind(py);
            if object.is_instance_of::<PyDict>() { ObjectRepr::Map }
            else if object.is_instance_of::<PyList>() || object.is_instance_of::<PyTuple>() { ObjectRepr::Seq }
            else if type_defines_iteration(&object.get_type()) { ObjectRepr::Iterable }
            else { ObjectRepr::Plain }
        })
    }

    fn get_value(self: &Arc<Self>, key: &Value) -> Option<Value> {
        self.lookup(key, false)
    }

    fn get_attribute(self: &Arc<Self>, key: &str) -> Option<Value> {
        self.lookup(&Value::from(key), true)
    }

    fn get_method(self: &Arc<Self>, name: &str) -> Option<Value> { self.get_attribute(name) }

    fn set_attribute(self: &Arc<Self>, key: &str, value: Value) -> Result<bool, Error> {
        Python::attach(|py| -> PyResult<bool> {
            let namespace = py.import("jinja2.utils")?.getattr("Namespace")?;
            if !self.object.bind(py).is_instance(&namespace)? {
                let exception = py.import("jinja2.exceptions")?.getattr("TemplateRuntimeError")?
                    .call1(("cannot assign attribute on non-namespace object",))?;
                return Err(PyErr::from_value(exception));
            }
            self.object.bind(py).set_item(key, self.bridge.python(py, &value)?)?;
            Ok(true)
        }).map_err(|error| self.bridge.remember(error))
    }

    fn call(self: &Arc<Self>, state: &State, args: &[Value]) -> Result<Value, Error> {
        Python::attach(|py| self.bridge.invoke(self.object.bind(py), state, args, false))
            .map_err(|error| self.bridge.remember(error))
    }

    fn call_method(self: &Arc<Self>, state: &State, name: &str, args: &[Value]) -> Result<Value, Error> {
        let value = self.get_attribute(name).unwrap_or(Value::UNDEFINED);
        Python::attach(|py| self.bridge.python(py, &value)
            .and_then(|callback| self.bridge.invoke(callback.bind(py), state, args, false)))
            .map_err(|error| self.bridge.remember(error))
    }

    fn enumerate(self: &Arc<Self>) -> Enumerator {
        Python::attach(|py| {
            let object = self.object.bind(py);
            // Python iterators preserve live list growth/shrinkage. The VM now
            // obtains loop length lazily and peeks only when metadata asks.
            match object.try_iter() {
            Ok(iterator) => Enumerator::Iter(Box::new(PythonIterator {
                iterator: iterator.into_any().unbind(), bridge: self.bridge.clone(), done: false, mode: IteratorMode::Synchronous, after: None, async_stop: None, adapted_class: None })),
            Err(error) => Enumerator::Values(vec![Value::from(self.bridge.remember(error))]),
            }
        })
    }

    fn loop_iterator(self: &Arc<Self>, with_metadata: bool) -> Option<Result<Box<dyn minijinja::value::LoopIterator>, Error>> {
        if !self.bridge.asynchronous {
            if !with_metadata { return None; }
            return Some(Python::attach(|py| Ok::<_, PyErr>(self.object.bind(py).try_iter()?.into_any().unbind()))
                .map(|iterator| Box::new(PythonIterator { iterator,
                    bridge: self.bridge.clone(), done: false, mode: IteratorMode::SynchronousLoop, after: None, async_stop: None, adapted_class: None })
                    as Box<dyn minijinja::value::LoopIterator>)
                .map_err(|error| self.bridge.remember(error)));
        }
        async_loop_iterator(&self.bridge, &self.object, with_metadata, None)
    }

    fn enumerator_len(self: &Arc<Self>) -> Option<usize> {
        Python::attach(|py| match self.object.bind(py).len() {
            Ok(length) => Some(length),
            Err(error) if error.is_instance_of::<PyTypeError>(py) => None,
            Err(error) => { self.bridge.remember(error); None }
        })
    }

    fn enumerator_len_fallible(self: &Arc<Self>) -> Result<Option<usize>, Error> {
        Python::attach(|py| match self.object.bind(py).len() {
            Ok(length) => Ok(Some(length)),
            Err(error) if error.is_instance_of::<PyTypeError>(py) => Ok(None),
            Err(error) => Err(self.bridge.remember(error)),
        })
    }

    fn loop_length_value(self: &Arc<Self>) -> Result<Option<Value>, Error> {
        Python::attach(|py| {
            let call = (|| -> PyResult<Bound<'_, PyAny>> {
                let binding = crate::python_function::binding(py)?;
                let class = binding.getattr(if self.bridge.asynchronous {
                    "_RUNTIME_ASYNC_LOOP_TYPE"
                } else { "_RUNTIME_LOOP_TYPE" })?;
                let length = class.getattr("length")?.getattr("fget")?;
                let scope = crate::pyglobals::FrameGlobals::of_function(&length)?;
                crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "len"))?.call1((self.object.bind(py),))
            })();
            match call {
                Ok(length) => self.bridge.value(&length).map(Some).map_err(|error| self.bridge.remember(error)),
                Err(error) if error.is_instance_of::<PyTypeError>(py) => Ok(None),
                Err(error) => Err(self.bridge.remember(error)),
            }
        })
    }

    fn loop_length_subtract(self: &Arc<Self>, length: &Value, index: &Value) -> Result<Value, Error> {
        Python::attach(|py| -> PyResult<Value> {
            let length = self.bridge.python(py, length)?;
            let index = self.bridge.python(py, index)?;
            let result = unsafe {
                Bound::from_owned_ptr_or_err(py, pyo3::ffi::PyNumber_Subtract(length.bind(py).as_ptr(), index.bind(py).as_ptr()))?
            };
            self.bridge.value(&result)
        }).map_err(|error| self.bridge.remember(error))
    }

    fn loop_length_add(self: &Arc<Self>, left: &Value, right: &Value) -> Result<Value, Error> {
        Python::attach(|py| -> PyResult<Value> {
            let left = self.bridge.python(py, left)?;
            let right = self.bridge.python(py, right)?;
            let result = unsafe {
                Bound::from_owned_ptr_or_err(py, pyo3::ffi::PyNumber_Add(left.bind(py).as_ptr(), right.bind(py).as_ptr()))?
            };
            self.bridge.value(&result)
        }).map_err(|error| self.bridge.remember(error))
    }

    fn is_true(self: &Arc<Self>) -> bool {
        Python::attach(|py| self.object.bind(py).is_truthy())
            .unwrap_or_else(|error| { self.bridge.remember(error); false })
    }

    fn is_true_fallible(self: &Arc<Self>) -> Result<bool, Error> {
        Python::attach(|py| self.object.bind(py).is_truthy())
            .map_err(|error| self.bridge.remember(error))
    }

    fn render(self: &Arc<Self>, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        Python::attach(|py| match self.object.bind(py).str() {
            Ok(text) => f.write_str(&text.to_string_lossy()),
            Err(error) => { self.bridge.remember(error); Err(fmt::Error) }
        })
    }
}

pub fn configure(env: &mut Environment<'static>, bridge: &Arc<Bridge>) -> PyResult<()> {
    // MiniJinja's debug snapshot resolves referenced names again while an
    // error propagates. Python mappings and Undefined constructors can have
    // observable effects, so keep debug builds consistent with release and
    // with the Python reference's original exception path.
    env.set_debug(false);
    static NEXT_COMPILATION: AtomicUsize = AtomicUsize::new(0);
    env.set_compilation_key(|name| format!("{}\u{1f}{}", name, NEXT_COMPILATION.fetch_add(1, Ordering::Relaxed)));
    let fragments = bridge.clone();
    env.set_fragment_joiner(move |parts, autoescape| crate::python_session::join_fragments(&fragments, parts, autoescape));
    let macros = bridge.clone();
    env.set_runtime_macro_call(move |state, value, args| crate::python_macro::call(&macros, state, value, args));
    let macros = bridge.clone();
    env.set_runtime_macro_factory(move |state, value| crate::python_macro::create_value(&macros, state, value));
    let contexts = bridge.clone();
    env.set_runtime_context_handler(move |state, event| Python::attach(|py| -> PyResult<Value> {
        use minijinja::{RuntimeContextEvent, RuntimeScope};
        let importing = matches!(&event, RuntimeContextEvent::PublishImports(_));
        match event {
            RuntimeContextEvent::Root => {
                let base = state.context_base();
                let context = base.downcast_object_ref::<PythonObject>()
                    .and_then(|object| object.activation.as_ref())
                    .ok_or_else(|| unsupported(py, "template activation has no Python context"))?;
                Ok(contexts.opaque_value(context.bind(py)))
            }
            RuntimeContextEvent::SelfReference => {
                let context = contexts.python(py, state.runtime_context())?;
                let constructor = state.template_metadata()
                    .and_then(|metadata| metadata.downcast_object_ref::<CompiledTemplateMetadata>())
                    .and_then(|metadata| metadata.runtime_imports.get()?.iter()
                        .find(|(name, _)| name == "TemplateReference").map(|(_, value)| value.clone_ref(py)))
                    .ok_or_else(|| pyo3::exceptions::PyNameError::new_err("name 'TemplateReference' is not defined"))?;
                let reference = crate::python_context::template_reference(py, constructor.bind(py), context.bind(py))?;
                Ok(contexts.opaque_value(&reference))
            }
            RuntimeContextEvent::Include => {
                let environment = contexts.config.bind(py).getattr("environment")?;
                let variables = contexts.visible(py, state)?;
                let names: Vec<String> = state.block_stacks().into_iter().map(|(name, _)| name.to_owned()).collect();
                let registry = crate::python_blocks::initial_blocks(py, &contexts, &names)?;
                let context = crate::python_context_helpers::new_context(py,
                    &py.import("jinja2.environment")?.getattr("new_context")?, &environment,
                    PyString::new(py, state.name()).as_any(), &registry, variables.bind(py), true,
                    &environment.getattr("globals")?, py.None().bind(py))?;
                crate::python_blocks::mark_registry(py, &context)?;
                Ok(contexts.opaque_value(&context))
            }
            RuntimeContextEvent::Namespace => {
                let context = contexts.python(py, state.runtime_context())?;
                let prepared_resolver = context.bind(py).getattr("resolve_or_missing")?.unbind();
                let prepared_undefined = contexts.config.bind(py).getattr("environment")?.getattr("undefined")?.unbind();
                // Keep VM-owned objects opaque. Enumerating loop fields here
                // would consume lookahead values before the include uses them.
                let fallbacks = state.callback_known_variables().into_iter().filter_map(|name| {
                    let value = state.callback_lookup(&name)?;
                    Bridge::needs_native_scope(&value).then(|| (name.into_owned(), value))
                }).collect();
                Ok(Value::from_object(PythonObject { object: context.bind(py).getattr("parent")?.unbind(),
                    activation: Some(context), fallbacks: Some(Arc::new(fallbacks)), bridge: contexts.clone(), root: true, undefined: Some(false), prepared_resolver: Some(prepared_resolver), prepared_undefined: Some(prepared_undefined), text: None, special: None, dynamic_image: false, special_classes: None }))
            }
            RuntimeContextEvent::Publish(values) | RuntimeContextEvent::PublishImports(values) => {
                if state.runtime_scope() == RuntimeScope::Root && !values.is_empty() {
                    let context = contexts.python(py, state.runtime_context())?;
                    let context = context.bind(py);
                    let variables = context.getattr("vars")?;
                    if values.len() == 1 {
                        variables.set_item(&values[0].0, contexts.python(py, &values[0].1)?)?;
                    } else {
                        let update = PyDict::new(py);
                        for (name, value) in &values { update.set_item(name, contexts.python(py, value)?)?; }
                        variables.call_method1("update", (update,))?;
                    }
                    let public: Vec<_> = values.iter().filter(|(name, _)| !name.starts_with('_')).map(|(name, _)| name).collect();
                    if public.len() == 1 {
                        context.getattr("exported_vars")?.call_method1(if importing { "discard" } else { "add" }, (public[0],))?;
                    } else if !public.is_empty() {
                        context.getattr("exported_vars")?.call_method1(if importing { "difference_update" } else { "update" }, (PyList::new(py, public)?,))?;
                    }
                }
                Ok(Value::UNDEFINED)
            }
            RuntimeContextEvent::ModuleExports(values) => crate::python_macro::with_state(&contexts, state, || {
                let context = contexts.python(py, state.runtime_context())?;
                let exported = crate::python_context_helpers::get_exported(py, context.bind(py))?;
                // TemplateModule copies the returned mapping into an ordinary
                // instance dictionary. Preserve update's mapping/pair protocol.
                let copied = PyDict::new(py);
                copied.call_method1("update", (exported,))?;
                let named: HashMap<_, _> = values.iter().map(|(name, value)| (name.as_str(), value)).collect();
                let macros: HashMap<_, _> = values.iter().filter_map(|(_, value)| {
                    let facade = value.downcast_object_ref::<minijinja::RuntimeMacro>()?
                        .foreign_value()?.downcast_object_ref::<PythonObject>()?;
                    Some((facade.object.as_ptr() as usize, value))
                }).collect();
                let mut public = Vec::with_capacity(copied.len());
                for (key, object) in copied.iter() {
                    let name = key.extract::<String>()?;
                    // Keep the native macro body alive when the exported Python
                    // macro is its existing facade, rather than creating a weak
                    // facade with no owning VM value after the include returns.
                    let same_name = match named.get(name.as_str()) {
                        Some(value) if contexts.python(py, value)?.bind(py).is(&object) => Some(*value),
                        _ => None,
                    };
                    let owner = same_name.or_else(|| macros.get(&(object.as_ptr() as usize)).copied());
                    let value = match owner {
                        Some(value) => value.clone(),
                        None => contexts.value(&object)?,
                    };
                    public.push((name, value));
                }
                Ok(Value::from_iter(public))
            }),
            RuntimeContextEvent::EnterAutoEscape(value) => {
                let context = contexts.python(py, state.runtime_context())?;
                let eval = context.bind(py).getattr("eval_ctx")?;
                let saved = crate::python_context::eval_context_save(py, &eval)?;
                eval.setattr("autoescape", contexts.python(py, &value)?)?;
                Ok(contexts.opaque_value(&saved))
            }
            RuntimeContextEvent::LeaveAutoEscape(saved) => {
                let context = contexts.python(py, state.runtime_context())?;
                crate::python_context::eval_context_revert(py, &context.bind(py).getattr("eval_ctx")?, contexts.python(py, &saved)?.bind(py))?;
                Ok(Value::UNDEFINED)
            }
            RuntimeContextEvent::ScopedBlock(values) => minijinja::with_host_state(state, || {
                let context = contexts.python(py, state.runtime_context())?;
                let locals = PyDict::new(py);
                for (name, value) in &values { locals.set_item(name, contexts.python(py, value)?)?; }
                let derived = context.bind(py).call_method1("derived", (locals,))?;
                Ok(contexts.opaque_value(&derived))
            }),
        }
    }).map_err(|error| contexts.remember(error)));
    let blocks = bridge.clone();
    env.set_runtime_block_dispatch(move |state, name, depth, output| {
        dispatch_replaced_block(&blocks, state, name, depth, output)
    });
    let required_blocks = bridge.clone();
    env.set_runtime_required_block_check(move |state, name, native_length| Python::attach(|py| crate::python_macro::with_state(&required_blocks, state, || -> PyResult<bool> {
        // Python resolves the callable before evaluating the context and stack.
        let selected = state.template_metadata()
            .and_then(|metadata| metadata.downcast_object_ref::<CompiledTemplateMetadata>())
            .and_then(|metadata| metadata.runtime_imports.get()?.iter()
                .find(|(key, _)| key == "len").map(|(_, value)| value.clone_ref(py)));
        let len = if let Some(selected) = selected { selected.into_bound(py) } else {
            let builtins = state.template_metadata()
                .and_then(|metadata| metadata.downcast_object_ref::<CompiledTemplateMetadata>())
                .and_then(|metadata| metadata.runtime_builtins.lock().unwrap().as_ref().map(|value| value.clone_ref(py)));
            // Non-function from_code adapters do not expose their eventual
            // execution globals. Retain the prior binding behavior until the
            // adapter can supply them, without invoking the reference renderer.
            let fallback;
            let builtins = if let Some(ref builtins) = builtins { builtins.bind(py) } else {
                fallback = unsafe { Bound::from_borrowed_ptr(py, pyo3::ffi::PyEval_GetBuiltins()) }.cast_into::<PyDict>()?;
                &fallback
            };
            builtins.get_item("len")?.ok_or_else(|| {
                let error = pyo3::exceptions::PyNameError::new_err("name 'len' is not defined");
                let _ = error.value(py).setattr("name", "len");
                error
            })?
        };
        let context = required_blocks.python(py, state.runtime_context())?;
        let blocks = context.bind(py).getattr("blocks")?;
        let stack = if blocks.is_exact_instance_of::<PyDict>() {
            match blocks.cast::<PyDict>()?.get_item(name)? {
                Some(stack) => Some(stack),
                // In a registry the binding populated, a missing entry was removed.
                None if crate::python_blocks::is_registry(py, context.bind(py))? =>
                    return Err(pyo3::exceptions::PyKeyError::new_err(name.to_owned())),
                None => None,
            }
        } else { Some(blocks.get_item(name)?) };
        let required = if let Some(stack) = stack {
            len.call1((stack,))?.rich_compare(1, pyo3::basic::CompareOp::Le)?.is_truthy()?
        } else { native_length <= 1 };
        if required {
            let constructor = state.template_metadata()
                .and_then(|metadata| metadata.downcast_object_ref::<CompiledTemplateMetadata>())
                .and_then(|metadata| metadata.runtime_imports.get()?.iter()
                    .find(|(key, _)| key == "TemplateRuntimeError")
                    .map(|(_, value)| value.clone_ref(py)))
                .ok_or_else(|| {
                    let error = pyo3::exceptions::PyNameError::new_err("name 'TemplateRuntimeError' is not defined");
                    let _ = error.value(py).setattr("name", "TemplateRuntimeError");
                    error
                })?;
            let exception = constructor.bind(py).call1((format!("Required block '{name}' not found"),))?;
            Err(PyErr::from_value(exception))
        } else { Ok(true) }
    })).map_err(|error| required_blocks.remember(error)));
    if bridge.asynchronous {
        let loop_bridge = bridge.clone();
        env.set_runtime_loop_iterator(move |state, iterable, metadata, filtered| {
            // A descriptor _to_iterator runs on the live loop object, which
            // python_loop::start_filtered builds at the loop's start; the
            // native iterator then has nothing to pull.
            if metadata && !filtered {
                let descriptor = Python::attach(|py| crate::python_loop::custom_to_iterator_descriptor(
                    &loop_bridge.lookup_binding.bind(py).getattr("_RUNTIME_ASYNC_LOOP_TYPE")?));
                match descriptor {
                    Ok(true) => return Some(Ok(Box::new(EmptyLoopIterator) as Box<dyn minijinja::value::LoopIterator>)),
                    Ok(false) => {}
                    Err(error) => return Some(Err(loop_bridge.remember(error))),
                }
            }
            let selected = Python::attach(|py| -> PyResult<Option<LoopFactory>> {
                let factory = if metadata && !filtered {
                    let binding = loop_bridge.lookup_binding.bind(py);
                    let class = binding.getattr("_RUNTIME_ASYNC_LOOP_TYPE")?;
                    let (method, changed) = select_async_to_iterator(binding, &class)?;
                    if changed {
                        return Ok(Some(LoopFactory::ToIterator(method.unbind())));
                    }
                    let scope = crate::pyglobals::FrameGlobals::of_function(&method)?;
                    let selected = crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "auto_aiter"))?;
                    let imported = state.template_metadata().cloned().map(LoopFactory::Imported);
                    if imported.as_ref().and_then(LoopFactory::raw).is_some_and(|value| value.bind(py).is(&selected)) {
                        drop(selected);
                        imported.expect("matching imported factory exists")
                    } else { LoopFactory::Live(selected.unbind()) }
                } else {
                    let Some(value) = state.template_metadata().cloned() else { return Ok(None); };
                    LoopFactory::Imported(value)
                };
                let Some(callback) = factory.raw() else { return Ok(None); };
                if crate::python_async::is_standard_auto_aiter(callback.bind(py))? && (metadata || filtered) {
                    Ok(None)
                } else { Ok(Some(factory)) }
            });
            match selected {
                Ok(None) => None,
                Err(error) => Some(Err(loop_bridge.remember(error))),
                Ok(Some(factory)) => Some(crate::python_macro::with_state(&loop_bridge, state, ||
                    crate::python_async::without_reentrant_suspension(|| Python::attach(|py| -> PyResult<Box<dyn minijinja::value::LoopIterator>> {
                        let callback = factory.raw().expect("selected factory remains available");
                        let value = loop_bridge.python(py, iterable)?;
                        if filtered && metadata {
                            // Jinja's filter generator calls the factory on the source
                            // when it starts, after the loop converted the generator
                            // (python_loop::start_filtered hands the loop over).
                            return Ok(Box::new(DeferredFilteredInput { source: Some(value),
                                callback: Some(factory), inner: None, bridge: loop_bridge.clone(), done: false }));
                        }
                        if filtered {
                            let source = callback.bind(py).call1((value,))?;
                            return Ok(Box::new(DeferredFilteredInput { source: Some(source.unbind()),
                                callback: Some(factory), inner: None, bridge: loop_bridge.clone(), done: false }));
                        }
                        if crate::python_async::is_standard_auto_aiter(callback.bind(py))? {
                            return async_loop_iterator(&loop_bridge, &value, metadata, Some(callback))
                                .expect("async iterator hook always handles input")
                                .map_err(|error| loop_bridge.take_error().unwrap_or_else(|| crate::error(error)));
                        }
                        let result = callback.bind(py).call1((value,))?;
                        let iterator = if metadata { result.unbind() } else { crate::python_async::async_for_iterator(&result)? };
                        Ok(Box::new(PythonIterator { iterator, bridge: loop_bridge.clone(), done: false,
                            mode: IteratorMode::Async { with_metadata: metadata }, after: None, async_stop: None, adapted_class: None }))
                    })).map_err(|error| loop_bridge.remember(error)))),
            }
        });
        env.set_runtime_loop_next(|frames, filtered_depth, next| {
            crate::python_async::with_render_frames(frames, filtered_depth, next)
        });
        let prepare = bridge.clone();
        env.set_runtime_prepare_await(move |state| ensure_auto_await_available(&prepare, state));
        let deferred_bridge = bridge.clone();
        env.set_runtime_deferred_super(move |state, execute| deferred_call(&deferred_bridge, state, execute));
        let deferred_bridge = bridge.clone();
        env.set_runtime_deferred_loop(move |state, execute| deferred_call(&deferred_bridge, state, execute));
        let await_bridge = bridge.clone();
        env.set_runtime_await(move |state, value| minijinja::with_host_state(state, || {
            ensure_auto_await_available(&await_bridge, state)?;
            let selected = Python::attach(|py| state.template_metadata()
                .and_then(|metadata| metadata.downcast_object_ref::<CompiledTemplateMetadata>())
                .and_then(|metadata| metadata.auto_await.lock().unwrap().as_ref().map(|value| value.clone_ref(py))));
            // Native VM objects are not Python awaitables. Converting them to
            // Python containers here destroys callable loop-method identity.
            if value.as_object().is_some()
                && value.downcast_object_ref::<PythonObject>().is_none()
                && value.downcast_object_ref::<minijinja::RuntimeMacro>()
                    .and_then(|value| value.foreign_value()).is_none()
                && Python::attach(|py| crate::python_async::is_standard_auto_await(py, selected.as_ref()))
                    .map_err(|error| await_bridge.remember(error))? {
                return Ok(value);
            }
            let object = Python::attach(|py| await_bridge.python(py, &value))
                .map_err(|error| await_bridge.remember(error))?;
            let result = crate::python_async::with_render_frames(state.render_frames(), state.filtered_loop_depth(), ||
                crate::python_macro::with_state(&await_bridge, state, || crate::python_async::auto_await(
                    object, selected, await_bridge.await_adapter_probe.load(Ordering::Relaxed))))
                .map_err(|error| await_bridge.remember(error))?;
            // After the await, possibly on another thread (see fiber_local).
            let converter = &await_bridge;
            crate::fiber_local::attach(move |py| converter.value(result.bind(py)))
                .map_err(|error| await_bridge.remember(error))
        }));
    }
    Python::attach(|py| -> PyResult<()> {
        let config = bridge.config.bind(py);
        env.set_preserve_test_results(true);
        env.set_loop_else_on_completed_iteration(true);
        env.set_respect_include_context(true);
        env.set_runtime_prepared_lookup(|target, name| {
            let object = target.downcast_object::<PythonObject>()?;
            if object.activation.is_none() { return None; }
            Some(object.lookup_with_prepared_missing(&Value::from(name.to_owned()), true, true))
        });
        let import_missing = bridge.clone();
        env.set_runtime_import_missing(move |state, template, name, line| Python::attach(|py| -> PyResult<Value> {
            let template = import_missing.python(py, template)?;
            let target = template.bind(py).repr()?.extract::<String>()?;
            let name_repr = PyString::new(py, name).repr()?.extract::<String>()?;
            let named = state.template_metadata().and_then(|metadata|
                metadata.downcast_object_ref::<CompiledTemplateMetadata>()).is_some_and(|metadata| metadata.named_source);
            let position = if named {
                let source_name = PyString::new(py, state.name()).repr()?.extract::<String>()?;
                format!("line {line} in {source_name}")
            } else { format!("line {line}") };
            let hint = format!("the template {target} (imported on {position}) does not export the requested name {name_repr}");
            let kwargs = PyDict::new(py);
            kwargs.set_item("name", name)?;
            let base = state.active_template_base();
            let selected = base.downcast_object_ref::<PythonObject>()
                .and_then(|object| object.prepared_undefined.as_ref())
                .ok_or_else(|| unsupported(py, "importing template has no selected undefined constructor"))?;
            import_missing.value(&selected.bind(py).call((hint,), Some(&kwargs))?)
        }).map_err(|error| import_missing.remember(error)));
        let root_missing = bridge.clone();
        env.set_runtime_root_missing(move |target, name| Python::attach(|py| -> PyResult<Value> {
            let Some(object) = target.downcast_object_ref::<PythonObject>() else { return Ok(Value::UNDEFINED); };
            let Some(undefined) = &object.prepared_undefined else { return Ok(Value::UNDEFINED); };
            Ok(Value::from_object(PreparedMissingRoot {
                bridge: root_missing.clone(), undefined: undefined.clone_ref(py),
                name: PyString::new(py, name).into_any().unbind(),
            }))
        }).map_err(|error| root_missing.remember(error)));
        let prepared_use = bridge.clone();
        env.set_runtime_prepared_use(move |value| {
            let Some(missing) = value.downcast_object_ref::<PreparedMissingRoot>() else { return Ok(value); };
            Python::attach(|py| -> PyResult<Value> {
                let object = missing.materialize(py)?;
                Ok(prepared_use.opaque_value(object.bind(py)))
            }).map_err(|error| prepared_use.remember(error))
        });
        let call_policy = bridge.clone();
        env.set_call_policy_factory(move |_name| Python::attach(|py| -> PyResult<bool> {
            call_policy.config.bind(py).getattr("environment")?.getattr("sandboxed")?.is_truthy()
        }).map_err(|error| call_policy.remember(error)));
        let self_guard = bridge.clone();
        let self_lookup = bridge.clone();
        env.set_runtime_self_block_prepare(move |state, name| crate::python_macro::with_state(&self_lookup, state, || Python::attach(|py| -> PyResult<Option<Value>> {
            let environment = self_lookup.config.bind(py).getattr("environment")?;
            let method = environment.getattr("getattr")?;
            let binding = self_lookup.lookup_binding.bind(py);
            let function = method.getattr("__func__").ok();
            let standard = match function {
                Some(function) => crate::python_function::is_standard_function(&function,
                    &binding.getattr("_ENV_GETATTR")?, &binding.getattr("_ENV_GETATTR_CODE")?,
                    binding.getattr("_ENVIRONMENT_MODULE")?.cast()?, "getattr")?,
                None => false,
            };
            let sandboxed = environment.getattr("sandboxed")?.is_truthy()?;
            if standard || sandboxed
                && method.getattr("__func__").is_ok_and(|function| binding.getattr("_SANDBOX_GETATTR").is_ok_and(|original| function.is(&original))) {
                // The native block call is equivalent only with the standard call
                // dispatch; otherwise the real BlockReference goes through it.
                // Read Context.call from the class: the dispatcher lookup on the
                // instance happens once, when the VM prepares the call.
                let context = self_lookup.python(py, state.runtime_context())?;
                let class_call = context.bind(py).get_type().getattr("call")?;
                let native_call = !sandboxed
                    && crate::python_function::is_standard_function(&class_call,
                        &binding.getattr("_CONTEXT_CALL")?, &binding.getattr("_CONTEXT_CALL_CODE")?,
                        binding.getattr("_RUNTIME_MODULE")?.cast()?, "call")?
                    && binding.getattr("_block_reference_call_is_standard")?.call0()?.is_truthy()?;
                if native_call { return Ok(None); }
            }
            let reference = state.lookup("self").ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("self reference is unavailable"))?;
            let reference = self_lookup.python(py, &reference)?;
            let selected = method.call1((reference, name))?;
            self_lookup.value(&selected).map(Some)
        })).map_err(|error| self_lookup.remember(error)));
        env.set_runtime_self_block_guard(move |_state, sandboxed| {
            if sandboxed {
                Err(self_guard.remember(Python::attach(|py| unsupported(py,
                    "sandboxed self block calls require a BlockReference runtime adapter"))))
            } else { Ok(()) }
        });
        let self_arguments = bridge.clone();
        env.set_runtime_self_block_arguments(move |state, args, sandboxed| Python::attach(|py| -> PyResult<()> {
            let (positional, keywords) = self_arguments.loop_arguments(py, state, args)?;
            let keywords = keywords.unwrap_or_else(|| PyDict::new(py));
            if sandboxed { return Ok(()); }
            // Context.call consumes these compiler-supplied keyword names.
            for name in ["_loop_vars", "_block_vars"] {
                keywords.call_method1("pop", (name, py.None()))?;
            }
            if positional.is_empty() && keywords.is_empty() { return Ok(()); }
            let binding = crate::python_function::binding(py)?;
            let function = binding.getattr("_RUNTIME_MODULE")?.getattr("BlockReference")?.getattr("__call__")?;
            if !function.is_exact_instance_of::<pyo3::types::PyFunction>() {
                return Err(unsupported(py, "custom BlockReference call requires a runtime adapter"));
            }
            let code = function.getattr("__code__")?;
            let positional_count: usize = code.getattr("co_argcount")?.extract()?;
            let positional_only: usize = code.getattr("co_posonlyargcount")?.extract()?;
            let keyword_only: usize = code.getattr("co_kwonlyargcount")?.extract()?;
            let flags: usize = code.getattr("co_flags")?.extract()?;
            if positional_count != 1 || positional_only != 0 || keyword_only != 0 || flags & 0x0c != 0 {
                return Err(unsupported(py, "custom BlockReference call requires a runtime adapter"));
            }
            let mut values = vec![py.None()];
            for value in &positional { values.push(self_arguments.python(py, value)?); }
            let values = PyTuple::new(py, values)?;
            crate::python_function::bind_fixed(&function, &values, &keywords)?;
            Err(unsupported(py, "custom BlockReference call requires a runtime adapter"))
        }).map_err(|error| self_arguments.remember(error)));
        let call_preparation = bridge.clone();
        env.set_runtime_prepare_call(move |state, sandboxed| Python::attach(|py| -> PyResult<Value> {
            let owner = if sandboxed {
                call_preparation.config.bind(py).getattr("environment")?.unbind()
            } else { call_preparation.python(py, state.runtime_context())? };
            let method = owner.bind(py).getattr("call")?;
            Ok(call_preparation.opaque_value(&method))
        }).map_err(|error| call_preparation.remember(error)));
        let attribute_prepare = bridge.clone();
        let attribute_lookup = bridge.clone();
        env.set_runtime_attribute_handlers(
            move || Python::attach(|py| -> PyResult<Value> {
                let environment = attribute_prepare.config.bind(py).getattr("environment")?;
                Ok(attribute_prepare.opaque_value(&environment.getattr("getattr")?))
            }).map_err(|error| attribute_prepare.remember(error)),
            move |state, method, target, name| minijinja::with_host_state(state, || Python::attach(|py| -> PyResult<Option<Value>> {
                if target.downcast_object_ref::<minijinja::RuntimeLoop>().is_some() {
                    let method = attribute_lookup.python(py, method)?;
                    let Some(object) = custom_loop_lookup(&attribute_lookup, py,
                        crate::python_sandbox::guard_internal_loop(method.bind(py), true, attribute_lookup.asynchronous, false),
                        target)? else { return Ok(None); };
                    let key = PyString::new(py, name);
                    let value = crate::python_lookup::dispatch_lookup(method.bind(py), object.bind(py),
                        key.as_any(), true, attribute_lookup.lookup_binding.bind(py))?;
                    return attribute_lookup.value(&value).map(Some);
                }
                // the Undefined of an inline if without else is Python's
                // plain Undefined: its own lookups fail with Jinja's hint
                if target.downcast_object_ref::<PythonObject>().is_none()
                    && target.downcast_object_ref::<PreparedMissingRoot>().is_none()
                    && target.silent_undefined_line().is_none() { return Ok(None); }
                let object = attribute_lookup.python(py, target)?;
                let method = attribute_lookup.python(py, method)?;
                let key = PyString::new(py, name);
                let value = crate::python_lookup::dispatch_lookup(method.bind(py), object.bind(py),
                    key.as_any(), true, attribute_lookup.lookup_binding.bind(py))?;
                attribute_lookup.value(&value).map(Some)
            }).map_err(|error| attribute_lookup.remember(error))),
            move |value| Python::attach(|_| drop(value)),
        );
        let item_prepare = bridge.clone();
        let item_lookup = bridge.clone();
        env.set_runtime_item_handlers(
            move || Python::attach(|py| -> PyResult<Value> {
                let environment = item_prepare.config.bind(py).getattr("environment")?;
                Ok(item_prepare.opaque_value(&environment.getattr("getitem")?))
            }).map_err(|error| item_prepare.remember(error)),
            move |state, method, target, key| minijinja::with_host_state(state, || Python::attach(|py| -> PyResult<Option<Value>> {
                if target.downcast_object_ref::<minijinja::RuntimeLoop>().is_some() {
                    let method = item_lookup.python(py, method)?;
                    let standard = crate::python_sandbox::guard_internal_loop(method.bind(py), false, item_lookup.asynchronous, false)
                        .and_then(|()| if standard_loop_item_globals(&item_lookup, method.bind(py))? { Ok(()) } else {
                            Err(unsupported(py, "modified loop item lookup globals need a runtime adapter"))
                        });
                    let Some(object) = custom_loop_lookup(&item_lookup, py, standard, target)? else { return Ok(None); };
                    let key = item_lookup.python(py, key)?;
                    let value = crate::python_lookup::dispatch_lookup(method.bind(py), object.bind(py),
                        key.bind(py), false, item_lookup.lookup_binding.bind(py))?;
                    return item_lookup.value(&value).map(Some);
                }
                // the Undefined of an inline if without else is Python's
                // plain Undefined: its own lookups fail with Jinja's hint
                if target.downcast_object_ref::<PythonObject>().is_none()
                    && target.downcast_object_ref::<PreparedMissingRoot>().is_none()
                    && target.silent_undefined_line().is_none() { return Ok(None); }
                let object = item_lookup.python(py, target)?;
                let method = item_lookup.python(py, method)?;
                let key = item_lookup.python(py, key)?;
                let value = crate::python_lookup::dispatch_lookup(method.bind(py), object.bind(py),
                    key.bind(py), false, item_lookup.lookup_binding.bind(py))?;
                item_lookup.value(&value).map(Some)
            }).map_err(|error| item_lookup.remember(error))),
            move |value| Python::attach(|_| drop(value)),
        );
        let blocks_loaded = bridge.clone();
        env.set_runtime_blocks_loaded(move |state, appended| Python::attach(|py| -> PyResult<()> {
            let context = blocks_loaded.python(py, state.runtime_context())?;
            crate::python_blocks::loaded(py, &blocks_loaded, context.bind(py), appended)
        }).map_err(|error| blocks_loaded.remember(error)));
        let modules = bridge.clone();
        env.set_runtime_module_created(move |_state, module|
            Python::attach(|py| crate::python_module::created(py, &modules, module)).map_err(|error| modules.remember(error)));
        let host_error = bridge.clone();
        env.set_runtime_host_error(move |_state, class, message| {
            let error = Python::attach(|py| -> PyResult<PyErr> {
                let instance = py.import("jinja2.exceptions")?.getattr(class)?.call1((message,))?;
                Ok(PyErr::from_value(instance))
            });
            host_error.remember(error.unwrap_or_else(|error| error))
        });
        let include = bridge.clone();
        env.set_runtime_include_foreign(move |state, target, with_context| {
            if target.downcast_object_ref::<PythonObject>().is_none() { return Ok(None); }
            minijinja::with_host_state(state, || Python::attach(|py| -> PyResult<Option<String>> {
                let object = include.python(py, target)?;
                let template = object.bind(py);
                if !template.is_instance(&py.import("jinja2.environment")?.getattr("Template")?)? { return Ok(None); }
                if include.asynchronous || template.getattr("environment")?.getattr("is_async")?.is_truthy()? {
                    return Err(unsupported(py, "including a Template object in an async render needs an async adapter"));
                }
                // As Jinja's include of a Template: its root render function
                // with a context made from the caller's variables, or the body
                // of its default (cached) module without context.
                let events = if with_context {
                    let variables = include.visible(py, state)?;
                    let context = template.call_method1("new_context", (variables, true))?;
                    template.getattr("root_render_func")?.call1((context,))?
                } else {
                    template.call_method0("_get_default_module")?.getattr("_body_stream")?
                };
                Ok(Some(PyString::new(py, "").call_method1("join", (events,))?.extract::<String>()?))
            })).map_err(|error| include.remember(error))
        });
        let not_found = bridge.clone();
        env.set_runtime_template_not_found(move |_state, tried, select| {
            // Jinja's errors: TemplateNotFound(name), or TemplatesNotFound
            // for a list of candidates (with its own message when empty)
            let error = Python::attach(|py| -> PyResult<PyErr> {
                let exceptions = py.import("jinja2.exceptions")?;
                let names = tried.iter().map(|name| not_found.python(py, name)).collect::<PyResult<Vec<_>>>()?;
                let instance = if !select {
                    exceptions.getattr("TemplateNotFound")?.call1((names[0].clone_ref(py),))?
                } else if names.is_empty() {
                    let options = PyDict::new(py);
                    options.set_item("message", "Tried to select from an empty list of templates.")?;
                    exceptions.getattr("TemplatesNotFound")?.call((), Some(&options))?
                } else {
                    exceptions.getattr("TemplatesNotFound")?.call1((PyList::new(py, names)?,))?
                };
                Ok(PyErr::from_value(instance))
            });
            not_found.remember(error.unwrap_or_else(|error| error))
        });
        let constructed = bridge.clone();
        env.set_runtime_loop_constructed(move |_state| Python::attach(|py| -> PyResult<bool> {
            let binding = crate::python_function::binding(py)?;
            let class = binding.getattr(if constructed.asynchronous { "_RUNTIME_ASYNC_LOOP_TYPE" } else { "_RUNTIME_LOOP_TYPE" })?;
            crate::python_loop::loop_constructor_replaced(&class)
        }).map_err(|error| constructed.remember(error)));
        let handoff = bridge.clone();
        env.set_runtime_loop_handoff(move |state, target, field| Python::attach(|py| -> PyResult<()> {
            if field == "start" { return crate::python_loop::start_filtered(py, &handoff, target); }
            if matches!(field, "changed" | "cycle") {
                // A custom Context.call receives the live loop's bound method.
                let Some(dispatcher) = state.prepared_call() else { return Ok(()); };
                let dispatcher = handoff.python(py, &dispatcher)?;
                let sandboxed = state.call_policy() == Some(true);
                // A modified sandbox policy, method flag or loop class decides
                // on the live LoopContext's bound method, as in Jinja.
                let standard_policy = !sandboxed
                    || crate::python_sandbox::internal_loop_call_is_standard(dispatcher.bind(py), field, handoff.asynchronous)?;
                let context_call = if sandboxed {
                    handoff.python(py, state.runtime_context())?.bind(py).getattr("call")?.unbind()
                } else { dispatcher };
                if standard_policy && crate::python_call::standard_context_call(context_call.bind(py))? { return Ok(()); }
                let Some(source) = target.downcast_object::<minijinja::RuntimeLoop>() else { return Ok(()); };
                return crate::python_loop::live_loop(py, &handoff, &source).map(|_| ());
            }
            crate::python_loop::handoff_before_field(py, &handoff, target)
        }).map_err(|error| handoff.remember(error)));
        let loop_post_lookup = bridge.clone();
        env.set_runtime_loop_post_lookup(move |_state, dispatcher, _target, attribute, _key, value| Python::attach(|py| -> PyResult<Value> {
            // AsyncLoopContext's lookahead/length properties return a coroutine.
            // Jinja's sandbox approves that object before it is awaited, so
            // mutations during the await do not retroactively change approval.
            if loop_post_lookup.asynchronous { return Ok(value); }
            let dispatcher = loop_post_lookup.python(py, dispatcher)?;
            crate::python_sandbox::guard_internal_loop(dispatcher.bind(py), attribute.is_some(), loop_post_lookup.asynchronous, true)?;
            Ok(value)
        }).map_err(|error| loop_post_lookup.remember(error)));
        let loop_item_key = bridge.clone();
        env.set_runtime_loop_item_key(move |dispatcher, key| Python::attach(|py| -> PyResult<Option<Value>> {
            let key = loop_item_key.python(py, key)?;
            let key = key.bind(py);
            if !key.is_exact_instance_of::<PyString>() { return Ok(None); }
            let dispatcher = loop_item_key.python(py, dispatcher)?;
            if !standard_loop_item_globals(&loop_item_key, dispatcher.bind(py))? {
                return Err(unsupported(py, "modified loop item lookup globals need a runtime adapter"));
            }
            match key.extract::<String>() {
                Ok(key) => Ok(Some(Value::from(key))),
                Err(error) if error.is_instance_of::<pyo3::exceptions::PyUnicodeEncodeError>(py) => Ok(None),
                Err(error) => Err(error),
            }
        }).map_err(|error| loop_item_key.remember(error)));
        if bridge.guard_reserved_literals {
            let constant_guard = bridge.clone();
            env.set_runtime_constant_output_guard(move |value| Python::attach(|py| -> PyResult<()> {
                let value = constant_guard.python(py, value)?;
                guard_deferred_marker(&constant_guard, value.bind(py), false).map(|_| ())
            }).map_err(|error| constant_guard.remember(error)));
            let capture = bridge.clone();
            env.set_runtime_capture_wrapper(move |value| Python::attach(|py| -> PyResult<Value> {
                let value = capture.python(py, &value)?;
                Ok(capture.opaque_value(value.bind(py)))
            }).map_err(|error| capture.remember(error)));
        }
        let constants = bridge.clone();
        env.set_constant_output_values(move |name, _source, start, end, autoescape| Python::attach(|py| {
            let value = constants.config.bind(py).call_method1("constant_output", (name, start, end, autoescape))?;
            if value.is_none() { return Ok(None); }
            match value.extract::<String>() {
                Ok(text) => Ok(Some(minijinja::ConstantOutput::Text(text))),
                Err(error) if error.is_instance_of::<pyo3::exceptions::PyUnicodeEncodeError>(py) => {
                    Ok(Some(minijinja::ConstantOutput::Fragment(constants.opaque_value(&value))))
                }
                Err(error) => Err(error),
            }
        }).map_err(|error| constants.remember(error)));
        let arithmetic = bridge.clone();
        env.set_constant_arithmetic(move |name, operator, left, right, autoescape, nonfinite_source, only_jinja, preselected| Python::attach(|py| -> PyResult<Option<(Value, bool)>> {
            let context = arithmetic.config.bind(py).getattr("_compile_eval_contexts")?.get_item(name)?;
            let optimized = arithmetic.optimizer_plans.lock().unwrap().get(name).copied().unwrap_or(false);
            let python_fold = !optimized || autoescape.is_none() || context.getattr("volatile")?.is_truthy()?;
            if python_fold && only_jinja { return Ok(None); }
            // Jinja emits non-finite source literals as names, which CPython
            // cannot fold. Non-finite results of finite literals are constants.
            if python_fold && nonfinite_source { return Ok(None); }
            let environment = arithmetic.config.bind(py).getattr("environment")?;
            let token = match operator {
                "add" => "+", "sub" | "neg" => "-", "mul" => "*", "div" => "/",
                "int_div" => "//", "rem" => "%", "pow" => "**", "pos" => "+", _ => return Ok(None),
            };
            if !preselected && environment.getattr("sandboxed")?.is_truthy()?
                && environment.getattr(if right.is_none() { "intercepted_unops" } else { "intercepted_binops" })?.contains(token)? {
                return Ok(None);
            }
            let left = arithmetic.python(py, left)?;
            let right = right.map(|value| arithmetic.python(py, value)).transpose()?.unwrap_or_else(|| py.None());
            if python_fold && left.bind(py).is_instance_of::<PyInt>() && right.bind(py).is_instance_of::<PyInt>() {
                let bits = |value: &Bound<'_, PyAny>| -> PyResult<usize> { value.call_method0("bit_length")?.extract() };
                if operator == "mul" && bits(left.bind(py))?.saturating_add(bits(right.bind(py))?) > 128 {
                    return Ok(None);
                }
                if operator == "pow" {
                    let Ok(exponent) = right.bind(py).extract::<i128>() else { return Ok(None); };
                    if exponent >= 0 && bits(left.bind(py))?.saturating_mul(usize::try_from(exponent).unwrap_or(usize::MAX)) > 128 {
                        return Ok(None);
                    }
                }
            }
            let result = match crate::python_operators::python_operator(py, operator, left.bind(py), right.bind(py)) {
                Ok(value) => value,
                Err(error) if error.is_instance_of::<pyo3::exceptions::PyException>(py) => return Ok(None),
                Err(error) => return Err(error),
            };
            if result.is_exact_instance_of::<PyFloat>() { return Ok(Some((Value::from(result.extract::<f64>()?), python_fold))); }
            if result.is_exact_instance_of::<PyInt>() {
                if let Ok(value) = result.extract::<i128>() { return Ok(Some((Value::from(value), python_fold))); }
                if let Ok(value) = result.extract::<u128>() { return Ok(Some((Value::from(value), python_fold))); }
            }
            Ok(None)
        }).map_err(|error| arithmetic.remember(error)));
        let arithmetic_mode = bridge.clone();
        env.set_arithmetic_codegen_mode(move |name| {
            !arithmetic_mode.optimizer_plans.lock().unwrap().get(name).copied().unwrap_or(false)
        });
        let jinja_policy = bridge.clone();
        env.set_jinja_arithmetic_policy(move |name, token, unary, autoescape| Python::attach(|py| -> PyResult<Option<bool>> {
            let optimized = jinja_policy.optimizer_plans.lock().unwrap().get(name).copied().unwrap_or(false);
            if !optimized || autoescape.is_none() { return Ok(None); }
            if jinja_policy.config.bind(py).getattr("_simple_sources")?.contains(name)? { return Ok(None); }
            let context = jinja_policy.config.bind(py).getattr("_compile_eval_contexts")?.get_item(name)?;
            if context.getattr("volatile")?.is_truthy()? { return Ok(None); }
            let Some(token) = token else { return Ok(Some(false)); };
            let environment = jinja_policy.config.bind(py).getattr("environment")?;
            Ok(Some(environment.getattr("sandboxed")?.is_truthy()?
                && environment.getattr(if unary { "intercepted_unops" } else { "intercepted_binops" })?.contains(token)?))
        }).map_err(|error| jinja_policy.remember(error)));
        let constant_attribute = bridge.clone();
        env.set_jinja_const_attribute_lookup(move || Python::attach(|py| -> PyResult<Option<Value>> {
            let environment = constant_attribute.config.bind(py).getattr("environment")?;
            match environment.getattr("getattr") {
                Ok(method) => Ok(Some(constant_attribute.opaque_value(&method))),
                Err(error) if error.is_instance_of::<pyo3::exceptions::PyException>(py) => Ok(None),
                Err(error) => Err(error),
            }
        }).map_err(|error| constant_attribute.remember(error)));
        let constant_item = bridge.clone();
        env.set_jinja_const_item_lookup(move || Python::attach(|py| -> PyResult<Option<Value>> {
            let environment = constant_item.config.bind(py).getattr("environment")?;
            match environment.getattr("getitem") {
                Ok(method) => Ok(Some(constant_item.opaque_value(&method))),
                Err(error) if error.is_instance_of::<pyo3::exceptions::PyException>(py) => Ok(None),
                Err(error) => Err(error),
            }
        }).map_err(|error| constant_item.remember(error)));
        env.set_jinja_const_attribute_release(move |value| Python::attach(|_| drop(value)));
        let arguments = bridge.clone();
        env.set_constant_argument(move |template, name, input, autoescape| Python::attach(|py| -> PyResult<Option<minijinja::ConstantArgument>> {
            let Some(autoescape) = autoescape else { return Ok(None); };
            let environment = arguments.config.bind(py).getattr("environment")?;
            if !arguments.optimizer_plans.lock().unwrap().get(template).copied().unwrap_or(false) { return Ok(None); }
            let context = arguments.config.bind(py).getattr("_compile_eval_contexts")?.get_item(template)?;
            if context.getattr("volatile")?.is_truthy()? { return Ok(None); }
            context.setattr("autoescape", autoescape)?;
            for _ in 0..2 {
                let filter = environment.getattr("filters")?.call_method1("get", (name,))?;
                if filter.is_none() { continue; }
                let pass_arg = match filter.getattr("jinja_pass_arg") {
                    Ok(_) => Some(filter.getattr("jinja_pass_arg")?),
                    Err(error) if error.is_instance_of::<PyAttributeError>(py) => None,
                    Err(error) => return Err(error),
                };
                let pass_type = py.import("jinja2.utils")?.getattr("_PassArg")?;
                let context_arg = pass_type.getattr("context")?;
                let environment_arg = pass_type.getattr("environment")?;
                let eval_context_arg = pass_type.getattr("eval_context")?;
                if pass_arg.as_ref().is_some_and(|value| value.is(&context_arg)) { continue; }
                if arguments.asynchronous {
                    let variant = match filter.getattr("jinja_async_variant") {
                        Ok(value) => value.is_truthy()?,
                        Err(error) if error.is_instance_of::<PyAttributeError>(py) => false,
                        Err(error) => return Err(error),
                    };
                    if variant || py.import("inspect")?.call_method1("iscoroutinefunction", (&filter,))?.is_truthy()? { continue; }
                }
                let input = arguments.python(py, input)?;
                let result = if pass_arg.as_ref().is_some_and(|value| value.is(&environment_arg)) {
                    filter.call1((&environment, input))
                } else if pass_arg.as_ref().is_some_and(|value| value.is(&eval_context_arg)) {
                    filter.call1((&context, input))
                } else { filter.call1((input,)) };
                match result {
                    Ok(value) => {
                        if let Some(literal) = constant_argument(&value, &mut HashSet::new()) { return Ok(Some(literal)); }
                        if safe_constant_repr(&value, &mut HashSet::new())? {
                            let pool = arguments.constant_pools.lock().unwrap().entry(template.to_owned())
                                .or_insert_with(|| PyDict::new(py).unbind()).clone_ref(py);
                            let snapshot = ConstantSnapshot::capture(&value, pool.bind(py))?;
                            let factory = ForeignConstantFactory { snapshot, bridge: arguments.clone() };
                            return Ok(Some(minijinja::ConstantArgument::Factory(Value::from_object(factory))));
                        }
                    }
                    Err(error) if error.is_instance_of::<pyo3::exceptions::PyException>(py) => {},
                    Err(error) => return Err(error),
                }
            }
            // The reference compiler resolves the decorator once more when
            // emitting an unfurled filter, then uses that decision at runtime.
            let filter = environment.getattr("filters")?.call_method1("get", (name,))?;
            if !filter.is_none() {
                let marker = match filter.getattr("jinja_pass_arg") {
                    Ok(_) => Some(filter.getattr("jinja_pass_arg")?),
                    Err(error) if error.is_instance_of::<PyAttributeError>(py) => None,
                    Err(error) => return Err(error),
                };
                let context_arg = py.import("jinja2.utils")?.getattr("_PassArg")?.getattr("context")?;
                if marker.as_ref().is_some_and(|value| value.is(&context_arg)) {
                    let factory = ContextFilterArgument {
                        callback: filter.unbind(), input: arguments.python(py, input)?, bridge: arguments.clone(),
                    };
                    return Ok(Some(minijinja::ConstantArgument::Factory(Value::from_object(factory))));
                }
            }
            Ok(None)
        }).map_err(|error| arguments.remember(error)));
        let validator = bridge.clone();
        env.set_source_validator(move |name, source| Python::attach(|py| -> PyResult<()> {
            validator.constant_pools.lock().unwrap().remove(name);
            if !minijinja::compiling_loaded_template() {
                let builtins = template_builtins(py, &validator)?;
                validator.template_builtin_plans.lock().unwrap().insert(name.to_owned(), builtins);
            }
            let plan = validator.config.bind(py).call_method1("validate_source", (name, source))?.extract::<(bool, bool)>()?;
            let optimized = validator.config.bind(py).getattr("environment")?.getattr("optimized")?.is_truthy()?;
            validator.finalizer_plans.lock().unwrap().insert(name.to_owned(), plan);
            validator.optimizer_plans.lock().unwrap().insert(name.to_owned(), optimized);
            Ok(())
        }).map_err(|error| validator.remember(error)));
        let plans = bridge.clone();
        env.set_template_metadata(move |name| Python::attach(|py| -> PyResult<Value> {
            let (has_finalizer, needs_context) = plans.finalizer_plans.lock().unwrap().get(name).copied()
                .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("missing compiled finalizer plan"))?;
            let kind = plans.config.bind(py).getattr("_compile_finalizers")?.get_item(name)?.get_item(1)?.unbind();
            let constants = plans.constant_pools.lock().unwrap().entry(name.to_owned())
                .or_insert_with(|| PyDict::new(py).unbind()).clone_ref(py);
            let plan = plans.template_builtin_plans.lock().unwrap().remove(name);
            let prepared = plan.is_some();
            let (builtins, method) = match plan {
                Some(selection) => (selection.builtins, Some(selection.method)),
                None => (None, None),
            };
            Ok(Value::from_object(CompiledTemplateMetadata { named_source: minijinja::compiling_loaded_template(), has_finalizer, needs_context, kind, constants,
                auto_await: Mutex::new(None), auto_await_missing: AtomicBool::new(false),
                runtime_exports: Mutex::new(None), runtime_imports: OnceLock::new(),
                runtime_builtins: Mutex::new(builtins), runtime_builtins_prepared: prepared,
                runtime_from_code: Mutex::new(method) }))
        }).map_err(|error| plans.remember(error)));
        {
            let is_async = bridge.asynchronous;
            let prepare = bridge.clone();
            env.set_template_prelude(move |metadata| Python::attach(|py| -> PyResult<()> {
                let metadata = metadata.and_then(|value| value.downcast_object_ref::<CompiledTemplateMetadata>())
                    .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("missing compiled template metadata"))?;
                let names = runtime_export_names(py, prepare.lookup_binding.bind(py), is_async)?;
                *metadata.runtime_exports.lock().unwrap() = Some(names);
                Ok(())
            }).map_err(|error| prepare.remember(error)));
            let finish = bridge.clone();
            env.set_template_finalizer(move |metadata| Python::attach(|py| -> PyResult<()> {
                let metadata = metadata.and_then(|value| value.downcast_object_ref::<CompiledTemplateMetadata>())
                    .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("missing compiled template metadata"))?;
                if is_async { crate::python_async::initialize(py)?; }
                if !metadata.runtime_builtins_prepared {
                    let selection = template_builtins(py, &finish)?;
                    *metadata.runtime_builtins.lock().unwrap() = selection.builtins;
                    *metadata.runtime_from_code.lock().unwrap() = Some(selection.method);
                }
                let names = metadata.runtime_exports.lock().unwrap().clone()
                    .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("missing runtime export plan"))?;
                let names_tuple = PyTuple::new(py, &names)?;
                // A Python traceback retains the selected bound method while
                // the module import fails. Clone it outside the mutex so its
                // finalizer can safely reenter when that traceback is cleared.
                let method = metadata.runtime_from_code.lock().unwrap().as_ref()
                    .map(|method| method.clone_ref(py))
                    .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("missing selected from_code"))?;
                let binding = finish.lookup_binding.bind(py);
                let loader = binding.getattr("_load_native_runtime")?;
                let runtime = binding.getattr("_load_runtime_with_selected_method")?
                    .call1((loader, names_tuple, method))?;
                let resolve = Py::new(py, RuntimeImportResolver { module: Some(runtime.unbind()) })?;
                let retain = binding.getattr("_runtime_attribute_with_selected_method")?;
                let mut selected = None;
                let mut imports: Vec<(String, Py<PyAny>)> = Vec::with_capacity(names.len());
                for name in &names {
                    let method = metadata.runtime_from_code.lock().unwrap().as_ref()
                        .map(|method| method.clone_ref(py))
                        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("missing selected from_code"))?;
                    let value = retain.call1((&resolve, name, method))?;
                    if name == "auto_await" { selected = Some(value.unbind()); }
                    else if let Some((_, prior)) = imports.iter_mut().find(|(key, _)| key == name) {
                        *prior = value.unbind();
                    } else { imports.push((name.clone(), value.unbind())); }
                }
                metadata.auto_await_missing.store(selected.is_none(), Ordering::Relaxed);
                *metadata.auto_await.lock().unwrap() = selected;
                metadata.runtime_imports.set(imports).map_err(|_| PyRuntimeError::new_err("runtime imports already initialized"))?;
                // The imported module is released before the selected method;
                // both may run user finalizers and reenter the bridge.
                drop(resolve);
                let method = metadata.runtime_from_code.lock().unwrap().take();
                drop(method);
                Ok(())
            }).map_err(|error| finish.remember(error)));
        }
        let cleanup = bridge.clone();
        env.set_compilation_cleanup(move |name| Python::attach(|py| -> PyResult<()> {
            let finalizer = cleanup.finalizer_plans.lock().unwrap().remove(name);
            let optimizer = cleanup.optimizer_plans.lock().unwrap().remove(name);
            let pool = cleanup.constant_pools.lock().unwrap().remove(name);
            let builtins = cleanup.template_builtin_plans.lock().unwrap().remove(name);
            drop((finalizer, optimizer, pool, builtins));
            cleanup.config.bind(py).call_method1("release_compilation", (name,))?;
            Ok(())
        }).map_err(|error| cleanup.remember(error)));
        let syntax = bridge.clone();
        env.set_ast_adapter(move |name, _source| crate::python_ast::take_tree(&syntax, name));
        let operators = bridge.clone();
        env.set_operator_call_factory(move |name, token, unary| Python::attach(|py| -> PyResult<Option<Value>> {
            let environment = operators.config.bind(py).getattr("environment")?;
            if environment.getattr("sandboxed")?.is_truthy()?
                && environment.getattr(if unary { "intercepted_unops" } else { "intercepted_binops" })?.contains(token)? {
                // Reuse the template's constant pool so repeated operations
                // expose the same foreign string constant to custom policies.
                let pool = operators.constant_pools.lock().unwrap().entry(name.to_owned())
                    .or_insert_with(|| PyDict::new(py).unbind()).clone_ref(py);
                let token = match pool.bind(py).get_item(token)? {
                    Some(value) => value.cast_into::<PyString>()?,
                    None => {
                        let value = PyString::new(py, token);
                        pool.bind(py).set_item(&value, &value)?;
                        value
                    }
                };
                Ok(Some(Value::from_object(SandboxOperatorFactory { bridge: operators.clone(), token: token.unbind(), unary })))
            } else { Ok(None) }
        }).map_err(|error| operators.remember(error)));
        let operators = bridge.clone();
        env.set_runtime_loop_arguments(move |state, name, args| Python::attach(|py| -> PyResult<Vec<Value>> {
            let (positional, kwargs) = operators.loop_arguments(py, state, args)?;
            if state.call_policy() == Some(true) {
                let prepared = state.prepared_call().ok_or_else(|| unsupported(py,
                    "sandbox loop call has no captured dispatcher"))?;
                let prepared = operators.python(py, &prepared)?;
                crate::python_sandbox::guard_internal_loop_call(prepared.bind(py), name, operators.asynchronous)?;
            }
            if let Some(kwargs) = kwargs {
                for key in ["_block_vars", "_loop_vars"] { kwargs.del_item(key).or_else(|error| {
                    if error.is_instance_of::<pyo3::exceptions::PyKeyError>(py) { Ok(()) } else { Err(error) }
                })?; }
                if let Some((key, _)) = kwargs.iter().next() {
                    let method = py.import("jinja2.runtime")?.getattr("LoopContext")?.getattr(name)?;
                    return Err(crate::python_keywords::argument_error(&method, &key,
                        "() got an unexpected keyword argument '")?);
                }
            }
            if name == "cycle" && positional.is_empty() {
                return Err(PyTypeError::new_err("no items for cycling given"));
            }
            Ok(positional)
        }).map_err(|error| operators.remember(error)));
        let recursion = bridge.clone();
        env.set_runtime_loop_recursion(move |state, called, args, recursive| Python::attach(|py| -> PyResult<Value> {
            let dispatcher = state.prepared_call().ok_or_else(|| unsupported(py,
                "loop call has no captured dispatcher"))?;
            let dispatcher = recursion.python(py, &dispatcher)?;
            let sandboxed = state.call_policy() == Some(true);
            if !sandboxed && !crate::python_call::standard_context_call(dispatcher.bind(py))? {
                // A custom Context.call receives the live LoopContext; its
                // __call__ recurses natively through the loop's _recurse.
                return python_loop_call(&recursion, py, state, called, args);
            }
            let (positional, kwargs) = recursion.loop_arguments(py, state, args)?;
            if sandboxed {
                // A modified policy or loop class decides on the live
                // LoopContext, through the sandbox's own call.
                if !crate::python_sandbox::internal_loop_call_is_standard(dispatcher.bind(py), "__call__", recursion.asynchronous)? {
                    return python_loop_call(&recursion, py, state, called, args);
                }
            } else {
                crate::python_sandbox::guard_internal_loop_class(recursion.lookup_binding.bind(py).as_any(), recursion.asynchronous)?;
            }
            let context_call = if sandboxed {
                recursion.python(py, state.runtime_context())?.bind(py).getattr("call")?.unbind()
            } else { dispatcher };
            if !crate::python_call::standard_context_call(context_call.bind(py))? {
                return python_loop_call(&recursion, py, state, called, args);
            }
            let kwargs = kwargs.unwrap_or_else(|| PyDict::new(py));
            let kwargs = if sandboxed {
                crate::python_call::bound_keywords(&context_call.bind(py).getattr("__func__")?, &kwargs)?
            } else { kwargs };
            for key in ["_block_vars", "_loop_vars"] { kwargs.del_item(key).or_else(|error| {
                if error.is_instance_of::<pyo3::exceptions::PyKeyError>(py) { Ok(()) } else { Err(error) }
            })?; }
            let mut arguments = vec![py.None().into_bound(py)];
            for value in &positional { arguments.push(recursion.python(py, value)?.into_bound(py)); }
            let arguments = PyTuple::new(py, arguments)?;
            let function = recursion.lookup_binding.bind(py).getattr("_RUNTIME_LOOP_CALL")?;
            let bound = crate::python_function::bind_fixed(&function, &arguments, &kwargs)?;
            if !recursive {
                return Err(PyTypeError::new_err("The loop must have the 'recursive' marker to be called recursively."));
            }
            Ok(recursion.opaque_value(&bound[1]))
        }).map_err(|error| recursion.remember(error)));
        let scope = bridge.clone();
        env.set_runtime_loop_changed_scope(move |state, execute| Python::attach(|py| {
            // Keep macro reentry available through comparison and the release
            // of values replaced by it. Suspension of this active fiber stays
            // explicitly guarded while the host callback borrows its state.
            crate::python_async::without_reentrant_suspension(|| crate::python_macro::with_state(&scope, state, || {
                // Standard sandbox call dispatch resolves Context.call before
                // invoking the loop method. Retain that selection through the
                // callback so later mutations do not change error handling.
                let sandbox_call = if state.call_policy() == Some(true) {
                    let selected = (|| -> PyResult<Py<PyAny>> {
                        let context = scope.python(py, state.runtime_context())?;
                        Ok(context.bind(py).getattr("call")?.unbind())
                    })().map_err(|error| scope.remember(error))?;
                    Some(selected)
                } else { None };
                match execute() {
                    Ok(value) => Ok(value),
                    Err(error) => {
                        let Some(original) = scope.take_error() else { return Err(error); };
                        let handled = (|| -> PyResult<Value> {
                            // Use the already selected Context.call; custom
                            // dispatchers still require a real loop adapter.
                            let dispatcher = if let Some(dispatcher) = sandbox_call.as_ref() {
                                dispatcher.clone_ref(py)
                            } else if state.call_policy() == Some(false) {
                                let Some(dispatcher) = state.prepared_call() else { return Err(original); };
                                scope.python(py, &dispatcher)?
                            } else { return Err(original); };
                            let dispatcher = dispatcher.bind(py);
                            if !crate::python_call::standard_context_call(dispatcher)? { return Err(original); }
                            let value = crate::python_call::handle_standard_context_error(dispatcher, original)?;
                            scope.value(&value)
                        })();
                        handled.map_err(|error| scope.remember(error))
                    }
                }
            }))
        }));
        let operators = bridge.clone();
        env.set_runtime_loop_changed_with_state(move |_state, previous, current| Python::attach(|py| -> PyResult<bool> {
            let previous = PyTuple::new(py, previous.iter().map(|value| operators.python(py, value)).collect::<PyResult<Vec<_>>>()?)?;
            let current = PyTuple::new(py, current.iter().map(|value| operators.python(py, value)).collect::<PyResult<Vec<_>>>()?)?;
            previous.ne(current)
        }).map_err(|error| operators.remember(error)));
        let operators = bridge.clone();
        env.set_runtime_operator(move |state, op, left, right| Python::attach(|py| -> PyResult<Value> {
            let left = operators.python(py, left)?;
            let right = right.map(|value| operators.python(py, value)).transpose()?.unwrap_or_else(|| py.None());
            let value = if op == "concat" {
                crate::python_operators::concat_values(py, vec![left.into_bound(py), right.into_bound(py)],
                    state.auto_escape() != AutoEscape::None)?
            } else {
                crate::python_operators::python_operator(py, op, left.bind(py), right.bind(py))?
            };
            operators.value(&value)
        }).map_err(|error| operators.remember(error)));
        let nonfinite = bridge.clone();
        env.set_runtime_nonfinite_constant(move |number| Python::attach(|py| -> PyResult<Value> {
            // Jinja emits the Python identifiers `inf` and `nan` for these
            // constants. They are resolved from builtins when code executes.
            let value = resolve_nonfinite(py, number)?;
            nonfinite.value(&value)
        }).map_err(|error| nonfinite.remember(error)));
        let tuple_constants = bridge.clone();
        env.set_runtime_subscript_tuple(move |state, value| Python::attach(|py| -> PyResult<Value> {
            let mut tuple = tuple_constants.python(py, &value)?.into_bound(py);
            let key = ("subscript_tuple", ConstantSnapshot::literal_key(&tuple)?).into_pyobject(py)?.into_any();
            let pool = state.template_metadata()
                .and_then(|value| value.downcast_object_ref::<CompiledTemplateMetadata>())
                .map(|plan| plan.constants.clone_ref(py))
                .unwrap_or_else(|| tuple_constants.constant_pools.lock().unwrap().entry(state.name().to_owned())
                    .or_insert_with(|| PyDict::new(py).unbind()).clone_ref(py));
            let pool = pool.bind(py);
            if let Some(existing) = pool.get_item(&key)? {
                return tuple_constants.value(&existing);
            }
            ConstantSnapshot::intern_literal(&mut tuple)?;
            pool.set_item(&key, &tuple)?;
            tuple_constants.value(&tuple)
        }).map_err(|error| tuple_constants.remember(error)));
        let unique_constants = bridge.clone();
        env.set_runtime_unique_constant(move |state, value, site| Python::attach(|py| -> PyResult<Value> {
            let key = ("unique_constant", site).into_pyobject(py)?.into_any();
            let pool = state.template_metadata()
                .and_then(|value| value.downcast_object_ref::<CompiledTemplateMetadata>())
                .map(|plan| plan.constants.clone_ref(py))
                .unwrap_or_else(|| unique_constants.constant_pools.lock().unwrap().entry(state.name().to_owned())
                    .or_insert_with(|| PyDict::new(py).unbind()).clone_ref(py));
            let pool = pool.bind(py);
            if let Some(existing) = pool.get_item(&key)? { return unique_constants.value(&existing); }
            let value = unique_constants.python(py, &value)?.into_bound(py);
            pool.set_item(&key, &value)?;
            unique_constants.value(&value)
        }).map_err(|error| unique_constants.remember(error)));
        let concat_bridge = bridge.clone();
        env.set_runtime_concat(move |state, values| Python::attach(|py| -> PyResult<Value> {
            let values = values.iter().map(|value| concat_bridge.python(py, value).map(|value| value.into_bound(py)))
                .collect::<PyResult<Vec<_>>>()?;
            let value = crate::python_operators::concat_values(py, values, state.auto_escape() != AutoEscape::None)?;
            concat_bridge.value(&value)
        }).map_err(|error| concat_bridge.remember(error)));
        let unpack_errors = bridge.clone();
        env.set_unpack_mismatch_handler(move |expected, actual| {
            let message = match actual {
                Some(actual) => format!("not enough values to unpack (expected {expected}, got {actual})"),
                None => format!("too many values to unpack (expected {expected})"),
            };
            unpack_errors.remember(PyValueError::new_err(message))
        });
        let iterator_errors = bridge.clone();
        env.set_unpack_iterator_error_handler(move |value, error| {
            if value.downcast_object_ref::<PythonObject>().is_none()
                && !matches!(value.kind(), ValueKind::None | ValueKind::Bool | ValueKind::Number) {
                return error;
            }
            let _ = Python::attach(|py| -> PyResult<()> {
                let compatible_error = iterator_errors.compatible_unpack_error(py);
                if !compatible_error { return Ok(()); }
                let object = iterator_errors.python(py, value)?;
                let object = object.bind(py);
                // CPython rewrites only the error for types with no iterator
                // or sequence protocol; user-defined iterator errors survive.
                let ty = unsafe { pyo3::ffi::Py_TYPE(object.as_ptr()) };
                let no_iter = unsafe { pyo3::ffi::PyType_GetSlot(ty, pyo3::ffi::Py_tp_iter).is_null() };
                let no_sequence = unsafe { pyo3::ffi::PySequence_Check(object.as_ptr()) == 0 };
                if !no_iter || !no_sequence { return Ok(()); }
                // CPython uses the type's tp_name and a 200-byte printf limit.
                let message = unsafe { Bound::from_owned_ptr_or_err(py,
                    pyo3::ffi::PyUnicode_FromFormat(c"cannot unpack non-iterable %.200s object".as_ptr(), (*ty).tp_name))? };
                let exception = py.get_type::<PyTypeError>().call1((message,))?;
                iterator_errors.replace_unpack_error(py, PyErr::from_value(exception));
                Ok(())
            });
            error
        });
        let unpack = bridge.clone();
        env.set_runtime_unpack_handler(move |value, count| {
            if value.downcast_object_ref::<PythonObject>().is_none()
                && !matches!(value.kind(), ValueKind::None | ValueKind::Bool | ValueKind::Number) {
                return None;
            }
            Some(Python::attach(|py| -> PyResult<RuntimeUnpack> {
                let object = unpack.python(py, value)?;
                let mut iterator = object.bind(py).try_iter()?;
                let mut raw = Vec::with_capacity(count.min(1024));
                for actual in 0..count {
                    match iterator.next() {
                        Some(Ok(item)) => raw.push(item.unbind()),
                        Some(Err(error)) => return Err(error),
                        None => return Ok(RuntimeUnpack::TooFew(actual)),
                    }
                }
                match iterator.next() {
                    Some(Ok(_)) => return Ok(RuntimeUnpack::TooMany),
                    Some(Err(error)) => return Err(error),
                    None => {},
                }
                let mut values = Vec::with_capacity(raw.len());
                for item in raw { values.push(unpack.unpacked_value(item.bind(py))?); }
                Ok(RuntimeUnpack::Values(values))
            }).map_err(|error| unpack.remember(error)))
        });
        let containers = bridge.clone();
        env.set_runtime_container_factory(move |container| Python::attach(|py| -> PyResult<Value> {
            let object = match container {
                RuntimeContainer::KeywordSources(sources) => {
                    return Ok(minijinja::value::Kwargs::from_sources(sources));
                }
                RuntimeContainer::List(values) => {
                    let list = PyList::empty(py);
                    for value in &values { list.append(containers.python(py, value)?)?; }
                    list.into_any()
                }
                RuntimeContainer::Tuple(values) => {
                    let values = values.iter().map(|v| containers.python(py, v)).collect::<PyResult<Vec<_>>>()?;
                    PyTuple::new(py, values)?.into_any()
                }
                RuntimeContainer::Map(values) => {
                    let mapping = PyDict::new(py);
                    for (key, value) in values { mapping.set_item(containers.python(py, &key)?, containers.python(py, &value)?)?; }
                    mapping.into_any()
                }
            };
            containers.value(&object)
        }).map_err(|error| containers.remember(error)));
        env.set_trim_blocks(config.getattr("trim_blocks")?.extract()?);
        env.set_lstrip_blocks(config.getattr("lstrip_blocks")?.extract()?);
        env.set_keep_trailing_newline(config.getattr("keep_trailing_newline")?.extract()?);
        env.set_syntax(syntax_config(&config.getattr("environment")?)?);
        let escape_bridge = bridge.clone();
        env.set_auto_escape_callback(move |name| Python::attach(|py| {
            let name = if name.starts_with("word/") || name.starts_with("docProps/") || name == "<string>" { None } else { Some(name) };
            match escape_bridge.config.bind(py).call_method1("autoescape", (name,)).and_then(|x| x.extract::<bool>()) {
                Ok(true) => AutoEscape::Html,
                Ok(false) => AutoEscape::None,
                Err(error) => { escape_bridge.remember(error); AutoEscape::None }
            }
        }));
        env.add_test("defined", |value: Value| -> Result<bool, Error> { Ok(!is_undefined(&value)?) });
        env.add_test("undefined", |value: Value| -> Result<bool, Error> { is_undefined(&value) });
        env.add_test("sameas", |left: Value, right: Value| {
            match (left.downcast_object_ref::<PythonObject>(), right.downcast_object_ref::<PythonObject>()) {
                (Some(left), Some(right)) => left.object.as_ptr() == right.object.as_ptr(),
                _ => left == right,
            }
        });
        env.add_filter("default", default_filter);
        env.add_filter("d", default_filter);
        for (attribute, test) in [("filters", false), ("tests", true)] {
            for (name, callback) in config.getattr(attribute)?.cast::<PyDict>()?.iter() {
                let name = name.extract::<String>()?;
                if test && crate::native_tests::install(env, bridge, &name, &callback)? {
                    continue;
                }
                let callback = callback.unbind();
                let local = bridge.clone();
                if test {
                    env.add_test(name, move |state: &State, args: Rest<Value>| -> Result<Value, Error> {
                        Python::attach(|py| local.invoke(callback.bind(py), state, &args, true))
                            .map_err(|error| local.remember(error))
                    });
                } else {
                    env.add_filter(name, move |state: &State, args: Rest<Value>| -> Result<Value, Error> {
                        Python::attach(|py| local.invoke(callback.bind(py), state, &args, true))
                            .map_err(|error| local.remember(error))
                    });
                }
            }
        }
        let loader = bridge.clone();
        env.set_loader(move |name| Python::attach(|py| loader.config.bind(py).call_method1("load", (name,))
            .and_then(|value| value.extract::<Option<String>>())).map_err(|error| loader.remember(error)));
        // Reuse a loaded template only when Jinja's cache would: the cache is
        // present and unchanged, and with auto_reload the loader's uptodate().
        let validator = bridge.clone();
        env.set_loaded_template_validator(move |name| Python::attach(|py| validator.config.bind(py)
            .call_method1("loaded_is_valid", (name,)).and_then(|value| value.is_truthy()))
            .map_err(|error| validator.remember(error)));
        let prepare = bridge.clone();
        env.set_output_preparer(move |state| {
            let (has_finalizer, needs_context) = state.template_metadata()
                .and_then(|value| value.downcast_object_ref::<CompiledTemplateMetadata>())
                .map(|plan| (plan.has_finalizer, plan.needs_context))
                .or_else(|| prepare.finalizer_plans.lock().unwrap().get(state.name()).copied())
                .ok_or_else(|| Error::new(ErrorKind::InvalidOperation, "missing compiled finalizer plan"))?;
            let callback = if has_finalizer {
                Some(Python::attach(|py| prepare.config.bind(py).getattr("environment")?.getattr("finalize")
                    .map(|value| value.unbind())).map_err(|error| prepare.remember(error))?)
            } else { None };
            Ok(Value::from_object(PreparedFormat { callback, has_finalizer, needs_context,
                autoescape: state.auto_escape() != AutoEscape::None }))
        });
        let formatter = bridge.clone();
        env.set_formatter(move |output, state, value| minijinja::with_host_state(state,
            || format_python_value(&formatter, output, state, value, None)));
        let formatter = bridge.clone();
        env.set_prepared_formatter(move |output, state, value, prepared| minijinja::with_host_state(state,
            || format_python_value(&formatter, output, state, value, Some(prepared))));
        // An isolated include has no root Python mapping through which to
        // resolve globals. Preserve the original objects in the environment.
        for (name, value) in config.getattr("globals")?.cast::<PyDict>()?.iter() {
            env.add_global(name.extract::<String>()?, bridge.value(&value)?);
        }
        *bridge.globals.lock().unwrap() = env.globals().map(|(name, _)| name.to_string()).collect();
        // A failing render leaves a template frame with its lexical values in
        // the traceback, as the reference does; see python_traceback.rs.
        crate::python_traceback::configure(env, bridge);
        Ok(())
    })
}

fn default_filter(value: Value, default: Option<Value>, boolean: Option<bool>, kwargs: minijinja::value::Kwargs) -> Result<Value, Error> {
    let default = kwargs.get::<Option<Value>>("default_value")?.or(default).unwrap_or_else(|| Value::from(""));
    let boolean = kwargs.get::<Option<bool>>("boolean")?.or(boolean).unwrap_or(false);
    kwargs.assert_all_used()?;
    Ok(if is_undefined(&value)? || (boolean && !value.is_true_fallible()?) { default } else { value })
}

pub(crate) fn is_undefined(value: &Value) -> Result<bool, Error> {
    if value.is_undefined() { return Ok(true); }
    let Some(object) = value.downcast_object_ref::<PythonObject>() else { return Ok(false); };
    match object.undefined {
        Some(undefined) => Ok(undefined),
        None => Python::attach(|py| -> PyResult<bool> {
            object.object.bind(py).is_instance(&py.import("jinja2")?.getattr("Undefined")?)
        }).map_err(|error| object.bridge.remember(error)),
    }
}
