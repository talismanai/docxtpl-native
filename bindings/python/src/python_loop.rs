//! A native loop that escapes to Python becomes a live `jinja2.runtime.LoopContext`.
//!
//! The first time a loop value crosses into Python (a callback argument, a
//! filter input, a finalizer), its native state is handed to a real
//! `LoopContext` of the reference runtime: index, length, lookahead, adjacent
//! items and the last `changed` arguments. From then on the VM advances the
//! loop and reads its attributes and methods through that object, so a callback
//! that keeps it, mutates `index0` or advances it is observed by the template,
//! and the same object is passed every time. Items not yet consumed stay in the
//! native iterator and are pulled by the object's `_iterator`.
use std::sync::{Arc, Weak};

use minijinja::value::Value;
use minijinja::{Error, LoopDelegate, LoopHandoff, RuntimeLoop, State};
use pyo3::exceptions::{PyAttributeError, PyStopAsyncIteration, PyStopIteration};
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::prelude::*;
use pyo3::types::PyTuple;

use crate::python_bridge::{unsupported, Bridge};

struct PythonLoop {
    // The LoopContext. Once the VM is done with the loop and only its
    // iterator (owned by the LoopContext) holds the loop, this becomes a weak
    // reference, so the LoopContext is freed as soon as Python drops it.
    object: SharedLoopObject,
    bridge: Arc<Bridge>,
    asynchronous: bool,
}

// Shared with the LoopContext's iterator, which reports the strong reference
// to the collector.
type SharedLoopObject = Arc<std::sync::Mutex<LoopObject>>;

enum LoopObject {
    Strong(Py<PyAny>),
    Weak(Py<pyo3::types::PyWeakrefReference>),
}

impl std::fmt::Debug for PythonLoop {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { f.write_str("PythonLoop") }
}

impl PythonLoop {
    fn object<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        match &*self.object.lock().unwrap() {
            LoopObject::Strong(object) => Ok(object.bind(py).clone()),
            LoopObject::Weak(reference) => {
                let object = reference.bind(py).call0()?;
                if object.is_none() { Err(unsupported(py, "the loop object was released after its loop ended")) }
                else { Ok(object) }
            }
        }
    }

    fn native(&self, py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Value> {
        let _ = py;
        self.bridge.value(value)
    }

    /// `async for` step: await `type(loop).__anext__(loop)` on the render's
    /// fiber, so a suspension happens at the native await boundary.
    fn advance_async(&self) -> Result<Option<Value>, Error> {
        let awaitable = Python::attach(|py| -> PyResult<Py<PyAny>> {
            let object = &self.object(py)?;
            let Some(slot) = crate::python_async::async_slot(object, pyo3::ffi::Py_am_anext) else {
                return Err(pyo3::exceptions::PyTypeError::new_err("loop object does not implement __anext__"));
            };
            let awaitable = unsafe { Bound::from_owned_ptr_or_err(py, slot(object.as_ptr()))? };
            crate::python_async::await_iterator(&awaitable)
        }).map_err(|error| self.bridge.remember(error))?;
        // After the await, possibly on another thread (see fiber_local).
        let result = crate::python_async::drive(awaitable);
        crate::fiber_local::attach(move |py| match result {
            Ok(pair) => (|| -> PyResult<Option<Value>> {
                let item = pair.bind(py).cast::<PyTuple>().map_err(PyErr::from).and_then(|pair| pair.get_item(0))?;
                self.native(py, &item).map(Some)
            })().map_err(|error| self.bridge.remember(error)),
            Err(error) if error.is_instance_of::<PyStopAsyncIteration>(py) => Ok(None),
            Err(error) => Err(self.bridge.remember(error)),
        })
    }
}

impl LoopDelegate for PythonLoop {
    fn advance(&self) -> Result<Option<Value>, Error> {
        if self.asynchronous { return self.advance_async(); }
        Python::attach(|py| -> PyResult<Option<Value>> {
            let object = self.object(py)?;
            let next = unsafe { pyo3::ffi::PyIter_Next(object.as_ptr()) };
            if next.is_null() {
                return match PyErr::take(py) {
                    Some(error) if error.is_instance_of::<PyStopIteration>(py) => Ok(None),
                    Some(error) => Err(error),
                    None => Ok(None),
                };
            }
            let pair = unsafe { Bound::from_owned_ptr(py, next) };
            let item = pair.cast::<PyTuple>().map_err(PyErr::from)
                .and_then(|pair| pair.get_item(0))?;
            self.native(py, &item).map(Some)
        }).map_err(|error| self.bridge.remember(error))
    }

    fn get(&self, key: &str) -> Result<Option<Value>, Error> {
        Python::attach(|py| -> PyResult<Option<Value>> {
            match self.object(py)?.getattr(key) {
                Ok(value) => self.native(py, &value).map(Some),
                Err(error) if error.is_instance_of::<PyAttributeError>(py) => Ok(None),
                Err(error) => Err(error),
            }
        }).map_err(|error| self.bridge.remember(error))
    }

    // As for any Python object: the bound method goes through the template's
    // call dispatch, so sandbox policies and Context.call see it.
    fn call_method(&self, state: &State, name: &str, args: &[Value]) -> Result<Value, Error> {
        Python::attach(|py| -> PyResult<Value> {
            let method = self.object(py)?.getattr(name)?;
            self.bridge.invoke(&method, state, args, false)
        }).map_err(|error| self.bridge.remember(error))
    }

    fn value(&self) -> Value {
        Python::attach(|py| match self.object(py) {
            Ok(object) => self.bridge.opaque_value(&object),
            Err(_) => Value::UNDEFINED,
        })
    }

    fn released(&self, others: usize) {
        // Only the LoopContext's iterator still holds the loop: drop the
        // strong reference that made the pair a cycle.
        if others != 1 { return; }
        Python::attach(|py| {
            let mut object = self.object.lock().unwrap();
            let LoopObject::Strong(strong) = &*object else { return; };
            let Ok(reference) = pyo3::types::PyWeakrefReference::new(strong.bind(py)) else {
                // A LoopContext replacement without weak references keeps the cycle.
                PyErr::take(py);
                return;
            };
            let previous = std::mem::replace(&mut *object, LoopObject::Weak(reference.unbind()));
            drop(object);
            drop(previous);
        });
    }
}

/// The loop's unconsumed items, as the `_iterator` of its `LoopContext`.
/// It owns the native loop, so a retained `LoopContext` can still advance it
/// after the render, as in Jinja. While the loop's delegate holds the
/// `LoopContext` strongly, that cycle crosses Rust: the collector is shown the
/// delegate's reference while this is the loop's only owner.
#[pyclass(module = "docxtpl_native._native")]
struct NativeLoopItems {
    source: Option<Arc<RuntimeLoop>>,
    // The delegate's reference to the context.
    delegated: Option<SharedLoopObject>,
    bridge: Arc<Bridge>,
}

impl Drop for NativeLoopItems {
    fn drop(&mut self) {
        // Jinja's LoopContext owns its iterator: replacing `_iterator` (as
        // `length` does) or dropping the context releases it, and its
        // finalizer runs then. Nothing else can pull these items.
        if let Some(source) = self.source.take() {
            Python::attach(|_| source.release_remaining());
        }
    }
}

impl NativeLoopItems {
    fn next_item(&self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        let Some(source) = self.source.clone() else { return Ok(None); };
        // Python frames are active here: an item source that would suspend
        // the render must fail instead of switching stacks under them.
        let item = crate::python_async::without_reentrant_suspension(|| source.next_remaining_item())
            .map_err(|error| self.bridge.take_error().unwrap_or_else(|| crate::error(error)))?;
        item.map(|item| self.bridge.python(py, &item)).transpose()
    }
}

#[pymethods]
impl NativeLoopItems {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let (Some(source), Some(delegated)) = (&self.source, &self.delegated)
            && Arc::strong_count(source) == 1
            // The loop's own reference and this temporary clone.
            && source.delegate().is_some_and(|delegate| Arc::strong_count(&delegate) == 2)
            && let Ok(object) = delegated.try_lock()
            && let LoopObject::Strong(object) = &*object {
            visit.call(object)?;
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.source = None;
        self.delegated = None;
    }

    fn __next__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.next_item(py)?.ok_or_else(|| PyStopIteration::new_err(()))
    }

    fn __aiter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }

    fn __anext__(slf: Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        // A callback of the active render pulls a filtered loop whose
        // predicates may suspend: run them on their own native coroutine,
        // from a copy of the render's state, as Jinja's filter generator
        // suspends independently of the render that is running.
        let this = slf.borrow();
        if this.bridge.asynchronous && crate::python_async::rendering()
            && let Some(source) = this.source.clone().filter(|source| source.is_filtered())
            // SAFETY: every bridge environment is an Environment<'static> (configure).
            && let Some(host) = unsafe { minijinja::detach_current_host_state() } {
            let bridge = this.bridge.clone();
            drop(this);
            return crate::python_async::native_coroutine(py, bridge.clone(), move || {
                let item = host.run(|state| {
                    let frames = state.render_frames();
                    let filtered_loop_depth = state.filtered_loop_depth();
                    crate::python_async::with_render_frames(frames, filtered_loop_depth, ||
                        minijinja::with_host_state(state, || source.next_remaining_item()))
                }).map_err(|error| bridge.take_error().unwrap_or_else(|| crate::error(error)))?;
                // After the predicates, possibly on another thread (see fiber_local).
                crate::fiber_local::attach(|py| match item {
                    Some(item) => bridge.python(py, &item),
                    None => Err(PyStopAsyncIteration::new_err(())),
                })
            });
        }
        drop(this);
        Ok(Py::new(py, NativeLoopNext { items: Some(slf.unbind()) })?.into_any())
    }
}

/// The awaitable of one `__anext__`: completes without suspending.
#[pyclass(module = "docxtpl_native._native")]
struct NativeLoopNext {
    items: Option<Py<NativeLoopItems>>,
}

#[pymethods]
impl NativeLoopNext {
    fn __await__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }

    fn __next__(&mut self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let Some(items) = self.items.take() else {
            return Err(pyo3::exceptions::PyRuntimeError::new_err("cannot reuse already awaited __anext__()"));
        };
        match items.bind(py).borrow().next_item(py)? {
            Some(item) => Err(PyStopIteration::new_err((item,))),
            None => Err(PyStopAsyncIteration::new_err(())),
        }
    }
}

/// The `_recurse` of a handed-over recursive loop: `loop(iterable)` from
/// Python renders the loop body natively, in the render that is running.
#[pyclass(module = "docxtpl_native._native")]
struct NativeLoopRecursion {
    source: Weak<RuntimeLoop>,
    bridge: Arc<Bridge>,
}

#[pymethods]
impl NativeLoopRecursion {
    #[pyo3(signature = (iterable, _recurse=None, depth=None))]
    fn __call__(&self, py: Python<'_>, iterable: &Bound<'_, PyAny>, _recurse: Option<&Bound<'_, PyAny>>,
        depth: Option<&Bound<'_, PyAny>>) -> PyResult<Py<PyAny>> {
        // Template code under this host call keeps the fiber on its thread.
        crate::python_async::pinned(move || -> PyResult<Py<PyAny>> {
        let _ = depth;
        let Some(source) = self.source.upgrade() else {
            return Err(unsupported(py, "a recursive loop can only be called during its render"));
        };
        let iterable = self.bridge.value(iterable)?;
        let value = source.recurse_from_host(iterable)
            .map_err(|error| self.bridge.take_error().unwrap_or_else(|| crate::error(error)))?;
        self.bridge.python(py, &value)
            })
    }
}

fn build(py: Python<'_>, bridge: &Arc<Bridge>, source: &Arc<RuntimeLoop>, handoff: LoopHandoff,
    convert: bool) -> PyResult<SharedLoopObject> {
    let runtime = crate::python_function::binding(py)?.getattr("_RUNTIME_MODULE")?;
    let class = runtime.getattr(if bridge.asynchronous { "AsyncLoopContext" } else { "LoopContext" })?;
    let missing = runtime.getattr("missing")?;
    let object = py.import("builtins")?.getattr("object")?.call_method1("__new__", (&class,))?;
    let undefined = bridge.config.bind(py).getattr("environment")?.getattr("undefined")?;
    let shared = Arc::new(std::sync::Mutex::new(LoopObject::Strong(object.clone().unbind())));
    let items = Py::new(py, NativeLoopItems { source: Some(source.clone()), delegated: Some(shared.clone()),
        bridge: bridge.clone() })?;
    // A filtered loop iterates a generator of its accepted items, as Jinja's
    // compiled `for ... if` does: no length, and iter() returns itself.
    if source.is_filtered() {
        let binding = crate::python_function::binding(py)?;
        let wrap = binding.getattr(if bridge.asynchronous { "_native_filtered_items_async" } else { "_native_filtered_items" })?;
        let generator = wrap.call1((items,))?;
        if bridge.asynchronous {
            // Jinja's loop started this async generator at its first step, so
            // the event loop's asyncgen hooks (firstiter, finalizer) know it and
            // close it at shutdown. Creating the first __anext__ awaitable
            // initializes them without running the generator.
            drop(generator.call_method0("__anext__")?);
        }
        // A replaced _to_iterator or auto_aiter converts the generator, as
        // AsyncLoopContext.__init__ does; the standard one returns it as is.
        // `self._to_iterator(iterable)`, read through the instance.
        let iterator = if convert { object.getattr("_to_iterator")?.call1((&generator,))? } else { generator.clone() };
        object.setattr("_iterable", &generator)?;
        object.setattr("_iterator", iterator)?;
    } else if convert {
        // AsyncLoopContext.__init__: _iterable, then self._to_iterator(iterable).
        let iterable = bridge.python(py, &handoff.iterable)?;
        object.setattr("_iterable", &iterable)?;
        object.setattr("_iterator", object.getattr("_to_iterator")?.call1((&iterable,))?)?;
    } else {
        object.setattr("_iterable", bridge.python(py, &handoff.iterable)?)?;
        object.setattr("_iterator", items)?;
    }
    object.setattr("_undefined", undefined)?;
    if handoff.recursive {
        object.setattr("_recurse", Py::new(py, NativeLoopRecursion { source: Arc::downgrade(source), bridge: bridge.clone() })?)?;
    } else {
        object.setattr("_recurse", py.None())?;
    }
    object.setattr("depth0", handoff.depth0)?;
    if let Some(index0) = handoff.index0 { object.setattr("index0", index0)?; }
    if let Some(length) = &handoff.length { object.setattr("_length", bridge.python(py, length)?)?; }
    let item = |value: &Option<Value>| -> PyResult<Py<PyAny>> {
        match value { Some(value) => bridge.python(py, value), None => Ok(missing.clone().unbind()) }
    };
    object.setattr("_after", item(&handoff.after)?)?;
    object.setattr("_current", item(&handoff.current)?)?;
    object.setattr("_before", item(&handoff.previous)?)?;
    if let Some(values) = &handoff.last_changed {
        let values = values.iter().map(|value| bridge.python(py, value)).collect::<PyResult<Vec<_>>>()?;
        object.setattr("_last_changed_value", PyTuple::new(py, values)?)?;
    }
    Ok(shared)
}

/// The loop object built by a replaced `LoopContext.__init__`, called as
/// Jinja's compiled loop calls the class, at the loop's start: the iterable
/// (a filtered loop's generator of accepted items), `undefined` and, for a
/// recursive loop, the recursion callback and depth.
fn build_constructed(py: Python<'_>, bridge: &Arc<Bridge>, source: &Arc<RuntimeLoop>, handoff: LoopHandoff)
    -> PyResult<SharedLoopObject> {
    let binding = crate::python_function::binding(py)?;
    let runtime = binding.getattr("_RUNTIME_MODULE")?;
    let class = runtime.getattr(if bridge.asynchronous { "AsyncLoopContext" } else { "LoopContext" })?;
    let undefined = bridge.config.bind(py).getattr("environment")?.getattr("undefined")?;
    let items = Py::new(py, NativeLoopItems { source: Some(source.clone()), delegated: None, bridge: bridge.clone() })?;
    let iterable = if source.is_filtered() {
        let wrap = binding.getattr(if bridge.asynchronous { "_native_filtered_items_async" } else { "_native_filtered_items" })?;
        wrap.call1((&items,))?
    } else { bridge.python(py, &handoff.iterable)?.into_bound(py) };
    let mut arguments = vec![iterable, undefined];
    if handoff.recursive {
        arguments.push(Py::new(py, NativeLoopRecursion { source: Arc::downgrade(source), bridge: bridge.clone() })?.into_any().into_bound(py));
        arguments.push(handoff.depth0.into_pyobject(py)?.into_any());
    }
    let object = class.call1(PyTuple::new(py, arguments)?)?;
    let shared = Arc::new(std::sync::Mutex::new(LoopObject::Strong(object.unbind())));
    items.borrow_mut(py).delegated = Some(shared.clone());
    Ok(shared)
}

/// Whether `class.__init__` (AsyncLoopContext inherits LoopContext's) is no
/// longer the function LoopContext had when the bridge module was imported.
pub(crate) fn loop_constructor_replaced(class: &Bound<'_, PyAny>) -> PyResult<bool> {
    let constructor = class.getattr(pyo3::intern!(class.py(), "__init__"))?;
    Ok(!crate::python_async::is_original_loop_constructor(&constructor)?)
}

/// Whether the module globals that `LoopContext.length` and `_peek_next` use
/// (`len`, `list`, `next`, `iter`, `tuple`) are still the builtins, and the
/// loop classes are unchanged; then the native algorithms are equivalent.
fn reference_algorithms_standard(py: Python<'_>, bridge: &Bridge) -> PyResult<bool> {
    let binding = crate::python_function::binding(py)?;
    if crate::python_sandbox::guard_internal_loop_class(&binding, bridge.asynchronous).is_err() {
        return Ok(false);
    }
    let class = binding.getattr(if bridge.asynchronous { "_RUNTIME_ASYNC_LOOP_TYPE" } else { "_RUNTIME_LOOP_TYPE" })?;
    let length = class.getattr("length")?.getattr("fget")?;
    let scope = crate::pyglobals::FrameGlobals::of_function(&length)?;
    let builtins = py.import("builtins")?;
    for name in ["len", "list", "next", "iter", "tuple"] {
        let name = pyo3::types::PyString::new(py, name);
        if !crate::pyglobals::frame_global(&scope, &name)?.is(&builtins.getattr(&name)?) { return Ok(false); }
    }
    Ok(true)
}

/// Hand a filtered loop to its `LoopContext` before a field whose algorithm
/// consumes input is computed, when the reference algorithm was modified.
pub(crate) fn handoff_before_field(py: Python<'_>, bridge: &Arc<Bridge>, target: &Value) -> PyResult<()> {
    let Some(source) = target.downcast_object::<RuntimeLoop>() else { return Ok(()); };
    if reference_algorithms_standard(py, bridge)? { return Ok(()); }
    live_loop(py, bridge, &source).map(|_| ())
}

/// The live `LoopContext` for `source`, created on first use. `None` when the
/// loop stays native.
pub(crate) fn live_loop(py: Python<'_>, bridge: &Arc<Bridge>, source: &Arc<RuntimeLoop>) -> PyResult<Option<Py<PyAny>>> {
    live_loop_converting(py, bridge, source, false)
}

/// At the start of a filtered async loop: when `AsyncLoopContext._to_iterator`
/// or the `auto_aiter` it calls was replaced, hand the loop over now so the
/// replacement converts the generator of accepted items, as in Jinja.
pub(crate) fn start_filtered(py: Python<'_>, bridge: &Arc<Bridge>, target: &Value) -> PyResult<()> {
    let Some(source) = target.downcast_object::<RuntimeLoop>() else { return Ok(()); };
    let binding = crate::python_function::binding(py)?;
    // A replaced LoopContext constructor runs when the loop starts, as the
    // compiled loop builds its LoopContext before iterating.
    let sync_class = binding.getattr("_RUNTIME_LOOP_TYPE")?;
    if !bridge.asynchronous {
        if loop_constructor_replaced(&sync_class)? { live_loop_converting(py, bridge, &source, true)?; }
        return Ok(());
    }
    let class = binding.getattr("_RUNTIME_ASYNC_LOOP_TYPE")?;
    // A user descriptor as _to_iterator is bound to the loop object, as in
    // AsyncLoopContext.__init__: build the live loop now.
    if loop_constructor_replaced(&class)? || custom_to_iterator_descriptor(&class)? {
        live_loop_converting(py, bridge, &source, true)?;
        return Ok(());
    }
    if !source.is_filtered() { return Ok(()); }
    let (method, changed) = crate::python_bridge::select_async_to_iterator(&binding, &class)?;
    let custom = changed || {
        let scope = crate::pyglobals::FrameGlobals::of_function(&method)?;
        let selected = crate::pyglobals::frame_global(&scope, pyo3::intern!(py, "auto_aiter"))?;
        !crate::python_async::is_standard_auto_aiter(&selected)?
    };
    if custom { live_loop_converting(py, bridge, &source, true)?; }
    Ok(())
}

/// Whether the class's own `_to_iterator` is neither a function nor a
/// static or class method: a descriptor that needs the loop instance. The
/// class dictionary is read without invoking it.
pub(crate) fn custom_to_iterator_descriptor(class: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = class.py();
    let Some(member) = class.getattr(pyo3::intern!(py, "__dict__"))?.call_method1("get", ("_to_iterator",))
        .ok().filter(|member| !member.is_none()) else { return Ok(false); };
    Ok(!(member.is_instance_of::<pyo3::types::PyFunction>() || crate::python_bridge::is_static_or_class_method(&member)))
}

fn live_loop_converting(py: Python<'_>, bridge: &Arc<Bridge>, source: &Arc<RuntimeLoop>, convert: bool)
    -> PyResult<Option<Py<PyAny>>> {
    if let Some(delegate) = source.delegate() {
        return bridge.python(py, &delegate.value()).map(Some);
    }
    let mut failure = None;
    let class = crate::python_function::binding(py)?.getattr(
        if bridge.asynchronous { "_RUNTIME_ASYNC_LOOP_TYPE" } else { "_RUNTIME_LOOP_TYPE" })?;
    let constructed = loop_constructor_replaced(&class)?;
    let delegate = source.delegate_with(|handoff| {
        let built = if constructed { build_constructed(py, bridge, source, handoff) }
            else { build(py, bridge, source, handoff, convert) };
        match built {
            Ok(object) => Ok(Arc::new(PythonLoop { object, bridge: bridge.clone(), asynchronous: bridge.asynchronous })
                as Arc<dyn LoopDelegate>),
            Err(error) => {
                failure = Some(error);
                Err(Error::new(minijinja::ErrorKind::InvalidOperation, "loop handoff failed"))
            }
        }
    });
    match delegate {
        Ok(delegate) => {
            // SAFETY: every bridge environment is an Environment<'static>.
            // A retained generator of accepted items can outlive the render.
            unsafe { source.retain_predicate_state() };
            bridge.python(py, &delegate.value()).map(Some)
        }
        Err(_) => match failure { Some(error) => Err(error), None => Ok(None) },
    }
}
