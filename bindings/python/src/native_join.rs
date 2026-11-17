//! Building blocks for Jinja's join filter. Python is retained only for
//! caller-owned objects and protocol callbacks.
use crate::pyglobals::{frame_global, FrameGlobals};
use crate::python_function::StandardFunction;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyModule, PyString, PyTuple, PyType};

static ATTRIBUTE_GETTER: pyo3::sync::PyOnceLock<StandardFunction> = pyo3::sync::PyOnceLock::new();
static ATTRIBUTE_PARTS: pyo3::sync::PyOnceLock<StandardFunction> = pyo3::sync::PyOnceLock::new();

struct JoinFunctions {
    wrapper: StandardFunction,
    synchronous: StandardFunction,
    asynchronous: StandardFunction,
    is_async: StandardFunction,
    auto_to_list: Option<StandardFunction>,
    auto_aiter: Option<StandardFunction>,
}

static JOIN: pyo3::sync::PyOnceLock<JoinFunctions> = pyo3::sync::PyOnceLock::new();

fn closure_value<'py>(function: &Bound<'py, PyAny>, index: usize) -> PyResult<Bound<'py, PyAny>> {
    function.getattr("__closure__")?.cast_into::<PyTuple>()?.get_item(index)?.getattr("cell_contents")
}

fn initialize_join(py: Python<'_>) -> PyResult<Option<JoinFunctions>> {
    let Some(filters) = crate::find_loaded_module(py, pyo3::intern!(py, "jinja2.filters"))? else { return Ok(None); };
    let Some(async_utils) = crate::find_loaded_module(py, pyo3::intern!(py, "jinja2.async_utils"))? else { return Ok(None); };
    let Ok(filters) = filters.bind(py).cast::<PyModule>() else { return Ok(None); };
    let Ok(async_utils) = async_utils.bind(py).cast::<PyModule>() else { return Ok(None); };
    let Some(wrapper) = filters.dict().get_item("do_join")? else { return Ok(None); };
    let Some(synchronous) = filters.dict().get_item("sync_do_join")? else { return Ok(None); };
    if !wrapper.is_exact_instance_of::<pyo3::types::PyFunction>()
        || !synchronous.is_exact_instance_of::<pyo3::types::PyFunction>() { return Ok(None); }
    let wrapper_code = wrapper.getattr("__code__")?;
    let freevars = wrapper_code.getattr("co_freevars")?.cast_into::<PyTuple>()?;
    let expected = ["async_func", "is_async", "need_eval_context", "normal_func"];
    if freevars.len() != expected.len() || freevars.iter().zip(expected).any(|(value, expected)| {
        !value.is_exact_instance_of::<PyString>() || !value.eq(expected).unwrap_or(false)
    }) { return Ok(None); }
    let is_async = match closure_value(&wrapper, 1) { Ok(value) => value, Err(_) => return Ok(None) };
    let asynchronous = match closure_value(&wrapper, 0) { Ok(value) => value, Err(_) => return Ok(None) };
    let need_eval = match closure_value(&wrapper, 2) { Ok(value) => value, Err(_) => return Ok(None) };
    let normal = match closure_value(&wrapper, 3) { Ok(value) => value, Err(_) => return Ok(None) };
    if !is_async.is_exact_instance_of::<pyo3::types::PyFunction>()
        || !asynchronous.is_exact_instance_of::<pyo3::types::PyFunction>() || !need_eval.is(&pyo3::types::PyBool::new(py, false))
        || !normal.is(&synchronous) { return Ok(None); }
    let wrapper = StandardFunction::capture_value(async_utils, "wrapper", &wrapper)?;
    let synchronous = StandardFunction::capture_value(filters, "sync_do_join", &synchronous)?;
    let asynchronous_standard = StandardFunction::capture_value(filters, "do_join", &asynchronous)?;
    let is_async_standard = StandardFunction::capture_value(async_utils, "is_async", &is_async)?;
    let auto_to_list = match async_utils.dict().get_item("auto_to_list")? {
        Some(function) if function.is_exact_instance_of::<pyo3::types::PyFunction>() => {
            let original = StandardFunction::capture_value(async_utils, "auto_to_list", &function)?;
            if original.matches(&function)? { Some(original) } else { None }
        }
        _ => None,
    };
    let auto_aiter = match async_utils.dict().get_item("auto_aiter")? {
        Some(function) if function.is_exact_instance_of::<pyo3::types::PyFunction>() => {
            let original = StandardFunction::capture_value(async_utils, "auto_aiter", &function)?;
            if original.matches(&function)? { Some(original) } else { None }
        }
        _ => None,
    };
    if !wrapper.matches(filters.dict().get_item("do_join")?.expect("observed above").as_any())?
        || !synchronous.matches(&normal)? || !asynchronous_standard.matches(&asynchronous)?
        || !is_async_standard.matches(&is_async)? { return Ok(None); }
    Ok(Some(JoinFunctions { wrapper, synchronous, asynchronous: asynchronous_standard,
        is_async: is_async_standard, auto_to_list, auto_aiter }))
}

fn loaded_closure<'py>(function: &Bound<'py, PyAny>, index: usize, name: &str) -> PyResult<Bound<'py, PyAny>> {
    closure_value(function, index).map_err(|_| pyo3::exceptions::PyNameError::new_err(
        format!("cannot access free variable '{name}' where it is not associated with a value in enclosing scope")))
}

fn global<'py>(scope: &FrameGlobals<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    frame_global(scope, &PyString::new(scope.py(), name))
}

/// The synchronous join algorithm, preserving live callback selection.
fn synchronous_join<'py>(function: &Bound<'py, PyAny>, args: &[Bound<'py, PyAny>]) -> PyResult<Bound<'py, PyAny>> {
    let scope = FrameGlobals::of_function(function)?;
    let eval_context = &args[0];
    let mut value = args[1].clone();
    let mut delimiter = args[2].clone();
    if !args[3].is_none() {
        // Python selects map before calling the getter factory. A replaced map
        // or string conversion can expose that getter to application code.
        let map = global(&scope, "map")?;
        let factory = global(&scope, "make_attrgetter")?;
        let environment = eval_context.getattr("environment")?;
        let observable = !map.is(&value.py().import("builtins")?.getattr("map")?)
            || !global(&scope, "str")?.is(&value.py().get_type::<PyString>());
        let getter = build_attribute_getter_mode(&factory, &environment, &args[3],
            value.py().None().bind(value.py()), value.py().None().bind(value.py()), observable)?;
        value = map.call1((getter, value))?;
    }
    if !eval_context.getattr("autoescape")?.is_truthy()? {
        let delimiter = global(&scope, "str")?.call1((delimiter,))?;
        let join = delimiter.getattr("join")?;
        let map = global(&scope, "map")?.call1((global(&scope, "str")?, value))?;
        return join.call1((map,));
    }
    if !global(&scope, "hasattr")?.call1((&delimiter, "__html__"))?.is_truthy()? {
        value = global(&scope, "list")?.call1((value,))?;
        let iterator = global(&scope, "enumerate")?.call1((&value,))?;
        let mut do_escape = false;
        for pair in iterator.try_iter()? {
            let (index, item) = crate::unpack_pair(&pair?)?;
            if global(&scope, "hasattr")?.call1((&item, "__html__"))?.is_truthy()? {
                do_escape = true;
            } else {
                let converted = global(&scope, "str")?.call1((item,))?;
                value.set_item(index, converted)?;
            }
        }
        delimiter = if do_escape {
            crate::native_filters::escape_value(&global(&scope, "escape")?, &delimiter)?
        } else { global(&scope, "str")?.call1((delimiter,))? };
        return delimiter.call_method1("join", (value,));
    }
    let soft_str = global(&scope, "soft_str")?;
    let delimiter = soft_str.call1((delimiter,))?;
    let join = delimiter.getattr("join")?;
    let map = global(&scope, "map")?.call1((global(&scope, "soft_str")?, value))?;
    join.call1((map,))
}

fn auto_aiter<'py>(function: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyAny>, bool)> {
    let scope = FrameGlobals::of_function(function)?;
    // The standard function's frame retains its iterable argument until return.
    let iterable = value.clone();
    if global(&scope, "hasattr")?.call1((&iterable, "__aiter__"))?.is_truthy()? {
        return Ok((iterable.call_method0("__aiter__")?, false));
    }
    let adapter = global(&scope, "_IteratorToAsyncIterator")?;
    let iterator = global(&scope, "iter")?.call1((&iterable,))?;
    let wrapper = if adapter.is_exact_instance_of::<PyType>()
        && crate::python_bridge::PythonIterator::adapter_constructor_is_standard(&adapter)? {
        let class = adapter.cast::<PyType>()?;
        let wrapper = unsafe { Bound::from_owned_ptr_or_err(value.py(),
            pyo3::ffi::PyType_GenericAlloc(class.as_type_ptr(), 0))? };
        wrapper.setattr("_iterator", iterator)?;
        wrapper
    } else {
        adapter.call1((iterator,))?
    };
    let binding = crate::python_function::binding(value.py())?;
    let original = binding.getattr("_ASYNC_ADAPTER_TYPE")?;
    let native_candidate = adapter.is(&original) && wrapper.get_type().is(&adapter);
    Ok((wrapper, native_candidate))
}

/// Execute the unchanged adapter method in Rust, preserving live globals and
/// exception context. A replaced method remains a Python callback.
fn standard_adapter_next(iterator: &Bound<'_, PyAny>) -> PyResult<Option<PyResult<Py<PyAny>>>> {
    let py = iterator.py();
    let class = iterator.get_type();
    let binding = crate::python_function::binding(py)?;
    if !class.is_exact_instance_of::<PyType>() || !class.is(&binding.getattr("_ASYNC_ADAPTER_TYPE")?) { return Ok(None); }
    let methods = class.getattr("__dict__")?;
    let method = match methods.get_item("__anext__") {
        Ok(method) => method,
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyKeyError>(py) => return Ok(None),
        Err(error) => return Err(error),
    };
    let original = binding.getattr("_ASYNC_ADAPTER_ANEXT")?;
    let code = binding.getattr("_ASYNC_ADAPTER_ANEXT_CODE")?;
    let module = binding.getattr("_ASYNC_UTILS_MODULE")?;
    if !crate::python_function::is_standard_function(
        &method, &original, &code, module.cast::<PyModule>()?, "__anext__")? {
        return Ok(None);
    }
    let scope = FrameGlobals::of_function(&method)?;
    // Python-to-Python calls transfer the argument stack reference into the
    // callee. A C vectorcall holds an additional reference while a Python
    // replacement runs, so retain the existing protocol path for overrides.
    let next = match global(&scope, "next") {
        Ok(next) => next,
        Err(_) => return Ok(None),
    };
    if !next.is(&binding.getattr("_BUILTIN_NEXT")?) { return Ok(None); }
    let next = (|| -> PyResult<Py<PyAny>> {
        let inner = iterator.getattr("_iterator")?;
        let arguments = [inner.as_ptr()];
        let result = unsafe { Bound::from_owned_ptr_or_err(py,
            pyo3::ffi::PyObject_Vectorcall(next.as_ptr(), arguments.as_ptr(), 1, std::ptr::null_mut()))? };
        Ok(result.unbind())
    })();
    Ok(Some(match next {
        Ok(item) => Ok(item),
        Err(error) => {
            let caught = error.clone_ref(py);
            crate::pyglobals::handle_exception(&scope, error, &["StopIteration"], || {
                let constructor = global(&scope, "StopAsyncIteration")?;
                let wrapped = PyErr::from_value(constructor.call1((caught.value(py).getattr("value")?,))?);
                wrapped.set_context(py, Some(caught.clone_ref(py)));
                wrapped.set_cause(py, Some(caught));
                Err(wrapped)
            })
        }
    }))
}

/// Native equivalent of the standard auto_to_list coroutine and comprehension.
fn collect_async<'py>(function: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    let scope = FrameGlobals::of_function(function)?;
    // auto_to_list's coroutine retains its value argument across iteration.
    let collection_value = value.clone();
    let callback = global(&scope, "auto_aiter")?;
    let standard = match JOIN.get(py).and_then(|original| original.auto_aiter.as_ref()) {
        Some(original) => original.matches(&callback)?,
        None => false,
    };
    let (source, adapter_candidate) = if standard { auto_aiter(&callback, &collection_value)? }
        else { (callback.call1((&collection_value,))?, false) };
    // The call operand is released before the async comprehension starts.
    drop(callback);
    let iterator = crate::python_async::async_for_iterator(&source)?;
    let adapter_candidate = adapter_candidate && iterator.bind(py).is(&source);
    drop(source);
    // In 3.11 the separate comprehension frame owns its .0 local as well as
    // the async-for stack entry. In 3.12 the comprehension is inlined.
    let comprehension_iterator = if py.version_info().minor <= 11 { Some(iterator.clone_ref(py)) } else { None };
    let result = PyList::empty(py);
    // The comprehension's loop variable retains the previous item while
    // __anext__ is called and awaited, even though the list owns it too.
    let mut previous: Option<Py<PyAny>> = None;
    loop {
        let item = (|| -> PyResult<Py<PyAny>> {
            let iterator = iterator.bind(py);
            if adapter_candidate {
                if let Some(result) = standard_adapter_next(iterator)? { return result; }
            }
            let Some(slot) = crate::python_async::async_slot(iterator, pyo3::ffi::Py_am_anext) else {
                return Err(pyo3::exceptions::PyTypeError::new_err(format!(
                    "'async for' requires an iterator with __anext__ method, got {}", iterator.get_type().name()?)));
            };
            let awaitable = unsafe { Bound::from_owned_ptr_or_err(py, slot(iterator.as_ptr()))? };
            let await_iterator = crate::python_async::await_iterator(&awaitable).map_err(|error| {
                let name = awaitable.get_type().name().map_or_else(|_| "unknown".to_owned(), |name| name.to_string());
                let wrapped = pyo3::exceptions::PyTypeError::new_err(format!(
                    "'async for' received an invalid object from __anext__: {name}"));
                wrapped.set_cause(py, Some(error));
                wrapped
            })?;
            crate::python_async::drive(await_iterator)
        })();
        match item {
            Ok(item) => {
                previous = Some(item);
                result.append(previous.as_ref().expect("just assigned"))?;
            }
            Err(error) if error.is_instance_of::<pyo3::exceptions::PyStopAsyncIteration>(py) => break,
            Err(error) => return Err(error),
        }
    }
    drop(previous);
    drop(comprehension_iterator);
    drop(collection_value);
    Ok(result.into_any())
}

#[pyclass(module = "docxtpl_native._native")]
struct JoinAwaitable {
    frame: Option<(Py<PyAny>, Vec<Py<PyAny>>)>,
}

#[pymethods]
impl JoinAwaitable {
    fn __await__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }

    fn __next__(&mut self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        // Template code under this host call keeps the fiber on its thread.
        crate::python_async::pinned(move || -> PyResult<Py<PyAny>> {
        let Some((function, arguments)) = self.frame.take() else {
            return Err(pyo3::exceptions::PyRuntimeError::new_err("cannot reuse already awaited coroutine"));
        };
        let result = (|| -> PyResult<Py<PyAny>> {
            let scope = FrameGlobals::of_function(function.bind(py))?;
            // Both globals are read before awaiting, as in the Jinja body.
            let synchronous = global(&scope, "sync_do_join")?;
            let collector = global(&scope, "auto_to_list")?;
            let standard = match JOIN.get(py).and_then(|original| original.auto_to_list.as_ref()) {
                Some(original) => original.matches(&collector)?,
                None => false,
            };
            let values = if standard {
                collect_async(&collector, arguments[1].bind(py))?
            } else {
                let iterable = collector.call1((arguments[1].bind(py),))?;
                crate::python_async::drive(crate::python_async::await_iterator(&iterable)?)?.into_bound(py)
            };
            let args = vec![arguments[0].bind(py).clone(), values,
                arguments[2].bind(py).clone(), arguments[3].bind(py).clone()];
            let standard = match JOIN.get(py) {
                Some(original) => original.synchronous.matches(&synchronous)?,
                None => false,
            };
            if standard {
                synchronous_join(&synchronous, &args).map(Bound::unbind)
            } else {
                synchronous.call1((args[0].clone(), args[1].clone(), args[2].clone(), args[3].clone())).map(Bound::unbind)
            }
        })().map_err(|error| crate::python_errors::escaped_iteration(py, error, crate::python_errors::Boundary::Coroutine))?;
        Err(pyo3::exceptions::PyStopIteration::new_err((result,)))
            })
    }

    fn __traverse__(&self, visit: pyo3::gc::PyVisit<'_>) -> Result<(), pyo3::gc::PyTraverseError> {
        if let Some((function, arguments)) = &self.frame {
            visit.call(function)?;
            for argument in arguments { visit.call(argument)?; }
        }
        Ok(())
    }

    fn __clear__(&mut self) { self.frame = None; }
}

pub(crate) fn try_join<'py>(function: &Bound<'py, PyAny>, args: &Bound<'py, PyTuple>,
    kwargs: &Bound<'py, PyDict>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = function.py();
    if JOIN.get(py).is_none() && function.is_exact_instance_of::<pyo3::types::PyFunction>()
        && function.getattr("__code__")?.getattr("co_name")?.eq("wrapper")? {
        if let Some(candidate) = initialize_join(py)? {
            JOIN.get_or_try_init(py, || Ok::<_, PyErr>(candidate))?;
        }
    }
    let Some(original) = JOIN.get(py) else { return Ok(None); };
    if !original.wrapper.matches(function)? { return Ok(None); }
    // Empty closure cells are left to CPython's original wrapper and error path.
    for index in 0..4 { if closure_value(function, index).is_err() { return Ok(None); } }
    let is_async = loaded_closure(function, 1, "is_async")?;
    let async_flag = if original.is_async.matches(&is_async)? {
        let scope = FrameGlobals::of_function(&is_async)?;
        let cast = global(&scope, "t")?.getattr("cast")?;
        let boolean = global(&scope, "bool")?;
        let flag = args.get_item(0)?.getattr("environment")?.getattr("is_async")?;
        if crate::native_filters::is_standard_cast(&cast)? { flag } else { cast.call1((boolean, flag))? }
    } else { is_async.call1((args,))? };
    let need_eval = loaded_closure(function, 2, "need_eval_context")?;
    let selected_args = if need_eval.is_truthy()? { args.get_slice(1, args.len()) } else { args.clone() };
    if async_flag.is_truthy()? {
        let async_function = loaded_closure(function, 0, "async_func")?;
        if original.asynchronous.matches(&async_function)? && (2..=4).contains(&selected_args.len()) {
            let bound = crate::python_function::bind_fixed(&async_function, &selected_args, kwargs)?;
            let frame = (async_function.unbind(), bound.into_iter().map(Bound::unbind).collect());
            return Ok(Some(Py::new(py, JoinAwaitable { frame: Some(frame) })?.into_bound(py).into_any()));
        }
        return async_function.call(&selected_args, Some(kwargs)).map(Some);
    }
    let synchronous = loaded_closure(function, 3, "normal_func")?;
    if !original.synchronous.matches(&synchronous)? || !(2..=4).contains(&selected_args.len()) {
        return synchronous.call(&selected_args, Some(kwargs)).map(Some);
    }
    let bound = crate::python_function::bind_fixed(&synchronous, &selected_args, kwargs)?;
    synchronous_join(&synchronous, &bound).map(Some)
}

fn capture_standard<'py>(module: &Bound<'py, PyModule>, name: &str,
    slot: &'static pyo3::sync::PyOnceLock<StandardFunction>) -> PyResult<Option<&'static StandardFunction>> {
    let py = module.py();
    if let Some(captured) = slot.get(py) { return Ok(Some(captured)); }
    let Some(function) = module.dict().get_item(name)? else { return Ok(None); };
    if !function.is_exact_instance_of::<pyo3::types::PyFunction>()
        || !function.getattr("__globals__")?.is(&module.dict()) { return Ok(None); }
    let code = function.getattr("__code__")?;
    let code_name = code.getattr("co_name")?;
    let filename = code.getattr("co_filename")?;
    let Some(module_file) = module.dict().get_item("__file__")? else { return Ok(None); };
    if !code_name.is_exact_instance_of::<PyString>() || !code_name.eq(name)?
        || !filename.is_exact_instance_of::<PyString>() || !module_file.is_exact_instance_of::<PyString>()
        || !filename.eq(&module_file)? { return Ok(None); }
    let original = StandardFunction::capture_value(module, name, &function)?;
    Ok(Some(slot.get_or_try_init(py, || Ok::<_, PyErr>(original))?))
}

fn prepare_parts<'py>(function: &Bound<'py, PyAny>, attribute: &Bound<'py, PyAny>)
    -> PyResult<Bound<'py, PyAny>> {
    let scope = FrameGlobals::of_function(function)?;
    let py = function.py();
    if attribute.is_none() { return Ok(PyList::empty(py).into_any()); }
    let isinstance = frame_global(&scope, pyo3::intern!(py, "isinstance"))?;
    let string = frame_global(&scope, pyo3::intern!(py, "str"))?;
    if !isinstance.call1((attribute, string))?.is_truthy()? {
        return Ok(PyList::new(py, [attribute])?.into_any());
    }
    let split = attribute.call_method1("split", (".",))?;
    let output = PyList::empty(py);
    for part in split.try_iter()? {
        let part = part?;
        let value = if part.call_method0("isdigit")?.is_truthy()? {
            frame_global(&scope, pyo3::intern!(py, "int"))?.call1((&part,))?
        } else { part };
        output.append(value)?;
    }
    Ok(output.into_any())
}

#[pyclass(name = "_JoinAttributeGetter", module = "docxtpl_native._native")]
struct JoinAttributeGetter {
    environment: Option<Py<PyAny>>,
    parts: Option<Py<PyAny>>,
    postprocess: Option<Py<PyAny>>,
    default: Option<Py<PyAny>>,
    function: Option<Py<PyAny>>,
}

#[pymethods]
impl JoinAttributeGetter {
    fn __call__<'py>(&self, py: Python<'py>, item: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let cleared = || pyo3::exceptions::PyRuntimeError::new_err("attribute getter was cleared");
        let environment = self.environment.as_ref().ok_or_else(cleared)?.bind(py);
        let parts = self.parts.as_ref().ok_or_else(cleared)?.bind(py);
        let postprocess = self.postprocess.as_ref().ok_or_else(cleared)?.bind(py);
        let default = self.default.as_ref().ok_or_else(cleared)?.bind(py);
        let function = self.function.as_ref().ok_or_else(cleared)?.bind(py);
        let mut result = item.clone();
        for part in parts.try_iter()? {
            result = crate::python_lookup::python_lookup(py, environment, &result, &part?, false)?;
            if !default.is_none() {
                let scope = FrameGlobals::of_function(function)?;
                if frame_global(&scope, pyo3::intern!(py, "isinstance"))?
                    .call1((&result, frame_global(&scope, pyo3::intern!(py, "Undefined"))?))?.is_truthy()? {
                    result = default.clone();
                }
            }
        }
        if !postprocess.is_none() { result = postprocess.call1((result,))?; }
        Ok(result)
    }

    fn __traverse__(&self, visit: pyo3::gc::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        visit.call(&self.environment)?;
        visit.call(&self.parts)?;
        visit.call(&self.postprocess)?;
        visit.call(&self.default)?;
        visit.call(&self.function)?;
        Ok(())
    }

    fn __clear__(&mut self) {
        self.environment.take();
        self.parts.take();
        self.postprocess.take();
        self.default.take();
        self.function.take();
    }
}

pub(crate) fn build_attribute_getter<'py>(factory: &Bound<'py, PyAny>, environment: &Bound<'py, PyAny>,
    attribute: &Bound<'py, PyAny>, postprocess: &Bound<'py, PyAny>, default: &Bound<'py, PyAny>)
    -> PyResult<Bound<'py, PyAny>> {
    build_attribute_getter_mode(factory, environment, attribute, postprocess, default, false)
}

fn build_attribute_getter_mode<'py>(factory: &Bound<'py, PyAny>, environment: &Bound<'py, PyAny>,
    attribute: &Bound<'py, PyAny>, postprocess: &Bound<'py, PyAny>, default: &Bound<'py, PyAny>, observable: bool)
    -> PyResult<Bound<'py, PyAny>> {
    let py = factory.py();
    let module = py.import("jinja2.filters")?;
    let standard = capture_standard(&module, "make_attrgetter", &ATTRIBUTE_GETTER)?;
    let recognized = match standard { Some(original) => original.matches(factory)?, None => false };
    if !recognized {
        return if postprocess.is_none() && default.is_none() { factory.call1((environment, attribute)) }
            else { factory.call1((environment, attribute, postprocess, default)) };
    }
    let scope = FrameGlobals::of_function(factory)?;
    let parts_function = frame_global(&scope, pyo3::intern!(py, "_prepare_attribute_parts"))?;
    let standard_parts = capture_standard(&module, "_prepare_attribute_parts", &ATTRIBUTE_PARTS)?;
    let parts = if let Some(original) = standard_parts {
        if original.matches(&parts_function)? { prepare_parts(&parts_function, attribute)? }
        else { parts_function.call1((attribute,))? }
    } else { parts_function.call1((attribute,))? };
    if observable {
        // A user callback receives the original function protocol, including
        // its code and closure. Construct it without executing the factory;
        // subsequent calls from application code remain application callbacks.
        let annotations = PyDict::new(py);
        annotations.set_item("item", global(&scope, "t")?.getattr("Any")?)?;
        annotations.set_item("return", global(&scope, "t")?.getattr("Any")?)?;
        let code = factory.getattr("__code__")?.getattr("co_consts")?.try_iter()?
            .filter_map(Result::ok).find(|value| value.getattr("co_name")
                .is_ok_and(|name| name.eq("attrgetter").unwrap_or(false)))
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("attribute getter code is unavailable"))?;
        let cells = code.getattr("co_freevars")?.try_iter()?.map(|name| {
            let value = match name?.extract::<String>()?.as_str() {
                "environment" => environment,
                "parts" => &parts,
                "postprocess" => postprocess,
                "default" => default,
                _ => return Err(pyo3::exceptions::PyRuntimeError::new_err("unknown attribute getter closure")),
            };
            unsafe { Bound::from_owned_ptr_or_err(py, pyo3::ffi::PyCell_New(value.as_ptr())) }
        }).collect::<PyResult<Vec<_>>>()?;
        let function = unsafe { Bound::from_owned_ptr_or_err(py,
            pyo3::ffi::PyFunction_New(code.as_ptr(), factory.getattr("__globals__")?.as_ptr()))? };
        function.setattr("__annotations__", annotations)?;
        let closure = PyTuple::new(py, cells)?;
        if unsafe { pyo3::ffi::PyFunction_SetClosure(function.as_ptr(), closure.as_ptr()) } != 0 {
            return Err(PyErr::fetch(py));
        }
        return Ok(function);
    }
    Ok(Py::new(py, JoinAttributeGetter {
        environment: Some(environment.clone().unbind()), parts: Some(parts.unbind()),
        postprocess: Some(postprocess.clone().unbind()), default: Some(default.clone().unbind()),
        function: Some(factory.clone().unbind()),
    })?.into_bound(py).into_any())
}

#[pyfunction(name = "_join_attribute_getter")]
#[pyo3(signature = (*args, **kwargs))]
fn join_attribute_getter<'py>(py: Python<'py>, args: &Bound<'py, PyTuple>,
    kwargs: Option<&Bound<'py, PyDict>>) -> PyResult<Bound<'py, PyAny>> {
    let module = py.import("jinja2.filters")?;
    let factory = module.getattr("make_attrgetter")?;
    let standard = capture_standard(&module, "make_attrgetter", &ATTRIBUTE_GETTER)?;
    if !match standard { Some(original) => original.matches(&factory)?, None => false } {
        return factory.call(args, kwargs);
    }
    let empty = PyDict::new(py);
    let values = crate::python_function::bind_fixed(&factory, args, kwargs.unwrap_or(&empty))?;
    build_attribute_getter(&factory, &values[0], &values[1], &values[2], &values[3])
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    if let Some(candidate) = initialize_join(module.py())? {
        JOIN.get_or_try_init(module.py(), || Ok::<_, PyErr>(candidate))?;
    }
    module.add_class::<JoinAttributeGetter>()?;
    module.add_function(wrap_pyfunction!(join_attribute_getter, module)?)
}
