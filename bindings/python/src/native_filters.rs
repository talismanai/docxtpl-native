//! Standard filter algorithms for Python-owned values. Only foreign protocols
//! and user overrides cross the binding; the Rust-only core uses MiniJinja.
use crate::pyglobals::{FrameGlobals, frame_global, handle_exception};
use crate::python_function::StandardFunction;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

const NAMES: &[&str] = &[
    "do_upper",
    "do_lower",
    "do_capitalize",
    "do_title",
    "do_format",
    "do_truncate",
    "do_filesizeformat",
    "do_center",
    "do_trim",
    "do_int",
    "do_float",
    "do_default",
    "do_round",
    "do_wordcount",
    "do_reverse",
    "do_last",
    "do_mark_safe",
    "do_mark_unsafe",
    "do_forceescape",
    "do_replace",
    "do_indent",
    "do_sort",
    "do_dictsort",
    "do_min",
    "do_max",
    "do_urlencode",
    "do_wordwrap",
    "do_pprint",
    "do_random",
    "do_attr",
    "do_batch",
    "do_items",
    "do_striptags",
    "do_xmlattr",
    "do_urlize",
];
// The synchronous bodies of async_variant filters, by filter name.
const SYNC_VARIANTS: &[(&str, &str)] = &[("do_first", "sync_do_first"), ("do_list", "sync_do_list"), ("do_sum", "sync_do_sum"),
    ("do_groupby", "sync_do_groupby"), ("do_slice", "sync_do_slice"), ("do_unique", "sync_do_unique"),
    ("do_select", "sync_do_select"), ("do_reject", "sync_do_reject"), ("do_selectattr", "sync_do_selectattr"),
    ("do_rejectattr", "sync_do_rejectattr"), ("do_map", "sync_do_map")];
// The async bodies of the async_variant filters run here for sync iterables:
// (filter name, the async function the wrapper holds, its code).
const ASYNC_PORTED: &[&str] = &["do_first", "do_list", "do_sum", "do_groupby", "do_slice", "do_unique", "do_select",
    "do_reject", "do_selectattr", "do_rejectattr", "do_map"];
type AsyncBody = (&'static str, Py<PyAny>, Py<PyAny>);
static ASYNC_FUNCTIONS: pyo3::sync::PyOnceLock<Vec<AsyncBody>> = pyo3::sync::PyOnceLock::new();
// auto_aiter, auto_to_list and the iterator adapter auto_aiter builds for a
// sync iterable (its class, __init__ and __anext__).
struct AsyncUtils { functions: Vec<(&'static str, StandardFunction)>, adapter: Py<PyAny>, methods: Vec<(&'static str, StandardFunction)> }
static ASYNC_UTILS: pyo3::sync::PyOnceLock<AsyncUtils> = pyo3::sync::PyOnceLock::new();
// The Environment methods select, reject and map call per item; an override
// is called instead of the native lookup.
const ENVIRONMENT_METHODS: &[&str] = &["call_filter", "call_test", "_filter_test_common"];
static ENVIRONMENT_FUNCTIONS: pyo3::sync::PyOnceLock<Vec<(&'static str, StandardFunction)>> =
    pyo3::sync::PyOnceLock::new();
static SYNC_FUNCTIONS: pyo3::sync::PyOnceLock<Vec<(&'static str, StandardFunction)>> =
    pyo3::sync::PyOnceLock::new();
// The async_variant wrappers themselves: (filter name, wrapper, its code).
type VariantWrapper = (&'static str, Py<PyAny>, Py<PyAny>);
static VARIANT_WRAPPERS: pyo3::sync::PyOnceLock<Vec<VariantWrapper>> = pyo3::sync::PyOnceLock::new();
// Module helpers the sort and min/max filters call; a replacement makes the
// filter run its Python body.
const HELPERS: &[&str] = &["ignore_case", "make_attrgetter", "make_multi_attrgetter",
    "_prepare_attribute_parts", "_min_or_max", "url_quote", "pformat", "select_or_reject", "prepare_select_or_reject",
    "prepare_map", "urlize", "async_select_or_reject"];
static HELPER_FUNCTIONS: pyo3::sync::PyOnceLock<Vec<(&'static str, StandardFunction)>> =
    pyo3::sync::PyOnceLock::new();
static FUNCTIONS: pyo3::sync::PyOnceLock<Vec<(&'static str, StandardFunction)>> =
    pyo3::sync::PyOnceLock::new();
static SOFT_STR: pyo3::sync::PyOnceLock<Option<StandardFunction>> = pyo3::sync::PyOnceLock::new();
static WORD_RE: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();
static CAST: pyo3::sync::PyOnceLock<Option<StandardFunction>> = pyo3::sync::PyOnceLock::new();
static ESCAPE: pyo3::sync::PyOnceLock<Option<StandardFunction>> = pyo3::sync::PyOnceLock::new();
static ESCAPE_INNER: pyo3::sync::PyOnceLock<Option<Py<PyAny>>> = pyo3::sync::PyOnceLock::new();
static ESCAPE_INNER_PY: pyo3::sync::PyOnceLock<Option<StandardFunction>> = pyo3::sync::PyOnceLock::new();
static MARKUP_REPLACE: pyo3::sync::PyOnceLock<StandardFunction> = pyo3::sync::PyOnceLock::new();
static MARKUP_ESCAPE: pyo3::sync::PyOnceLock<StandardFunction> = pyo3::sync::PyOnceLock::new();
static MARKUP_STRIPTAGS: pyo3::sync::PyOnceLock<StandardFunction> = pyo3::sync::PyOnceLock::new();
static MARKUP_UNESCAPE: pyo3::sync::PyOnceLock<StandardFunction> = pyo3::sync::PyOnceLock::new();
struct MarkupConstructor { class: Py<PyAny>, function: StandardFunction }
static MARKUP_CONSTRUCTOR: pyo3::sync::PyOnceLock<MarkupConstructor> = pyo3::sync::PyOnceLock::new();
static METHOD_TYPE: pyo3::sync::PyOnceLock<Option<Py<PyAny>>> = pyo3::sync::PyOnceLock::new();
static CLASSMETHOD_TYPE: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();

// A replacement compiled with the dependency's filename and function name is
// still a callback. Only the supported implementation's positional-only
// signature and global/attribute layout can enter its native algorithm.
fn escape_implementation_shape(function: &Bound<'_, PyAny>, inner: bool) -> PyResult<bool> {
    let code = function.getattr("__code__")?;
    let names = code.getattr("co_names")?.extract::<Vec<String>>()?;
    let expected: &[&str] = if inner { &["replace"] }
        else { &["type", "str", "Markup", "_escape_inner", "hasattr", "__html__"] };
    Ok(code.getattr("co_argcount")?.extract::<usize>()? == 1
        && code.getattr("co_posonlyargcount")?.extract::<usize>()? == 1
        && code.getattr("co_kwonlyargcount")?.extract::<usize>()? == 0
        && code.getattr("co_varnames")?.extract::<Vec<String>>()? == ["s"]
        && names == expected)
}

// PyO3 does not expose this CPython constructor. It creates a classmethod
// descriptor without consulting the mutable builtins.classmethod global.
unsafe extern "C" {
    fn PyClassMethod_New(callable: *mut pyo3::ffi::PyObject) -> *mut pyo3::ffi::PyObject;
    static mut PyStaticMethod_Type: pyo3::ffi::PyTypeObject;
}

// Read the class dictionary directly. A caller can replace Markup or install
// descriptors before the extension is imported; initialization must not run
// those descriptors merely to discover optimization candidates.
fn markup_member<'py>(py: Python<'py>, name: &str)
    -> PyResult<Option<(Bound<'py, pyo3::types::PyModule>, Bound<'py, PyAny>)>> {
    let module = py.import("markupsafe")?;
    let Some(mut class) = module.dict().get_item("Markup")? else { return Ok(None); };
    if !class.is_exact_instance_of::<pyo3::types::PyType>() {
        // Jinja keeps its own imported class even if MarkupSafe's global is
        // temporarily replaced. Reading the module dictionary avoids probing
        // the replacement or invoking its descriptors during initialization.
        let filters = py.import("jinja2.filters")?;
        let Some(jinja_class) = filters.dict().get_item("Markup")? else { return Ok(None); };
        class = jinja_class;
        if !class.is_exact_instance_of::<pyo3::types::PyType>() { return Ok(None); }
    }
    let module_name = class.getattr("__module__")?;
    let class_name = class.getattr("__name__")?;
    if !module_name.is_exact_instance_of::<pyo3::types::PyString>()
        || !class_name.is_exact_instance_of::<pyo3::types::PyString>()
        || !module_name.eq("markupsafe")?
        || !class_name.eq("Markup")? { return Ok(None); }
    let member = class.getattr("__dict__")?.call_method1("get", (name,))?;
    if member.is_none() { Ok(None) } else { Ok(Some((module, member))) }
}

// A temporarily replaced class must not permanently disable native dispatch.
// Retry discovery at the next use, without invoking attributes on replacements.
fn markup_standard(py: Python<'_>, name: &str) -> PyResult<Option<&'static StandardFunction>> {
    let (slot, classmethod) = match name {
        "replace" => (&MARKUP_REPLACE, false),
        "escape" => (&MARKUP_ESCAPE, true),
        "striptags" => (&MARKUP_STRIPTAGS, false),
        "unescape" => (&MARKUP_UNESCAPE, false),
        _ => unreachable!(),
    };
    if let Some(original) = slot.get(py) { return Ok(Some(original)); }
    let Some((module, descriptor)) = markup_member(py, name)? else { return Ok(None); };
    let function = if classmethod {
        if !CLASSMETHOD_TYPE.get(py).is_some_and(|class| descriptor.get_type().is(class.bind(py))) {
            return Ok(None);
        }
        descriptor.getattr("__func__")?
    } else { descriptor };
    if !function.is_exact_instance_of::<pyo3::types::PyFunction>() { return Ok(None); }
    // Do not freeze a user replacement as the standard method. Inspect only
    // exact builtin metadata objects, so discovery cannot call user equality.
    if !function.getattr("__globals__")?.is(&module.dict()) { return Ok(None); }
    let code = function.getattr("__code__")?;
    let filename = code.getattr("co_filename")?;
    let code_name = code.getattr("co_name")?;
    let Some(module_file) = module.dict().get_item("__file__")? else { return Ok(None); };
    if !filename.is_exact_instance_of::<pyo3::types::PyString>()
        || !code_name.is_exact_instance_of::<pyo3::types::PyString>()
        || !module_file.is_exact_instance_of::<pyo3::types::PyString>()
        || !filename.eq(&module_file)? || !code_name.eq(name)? { return Ok(None); }
    let original = StandardFunction::capture_value(&module, name, &function)?;
    Ok(Some(slot.get_or_try_init(py, || Ok::<_, PyErr>(original))?))
}

// Discover only the original one-argument Markup constructor. Replacements
// remain ordinary callbacks, and discovery can be retried after replacement.
fn standard_markup_constructor<'py>(markup: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = markup.py();
    if !markup.is_exact_instance_of::<pyo3::types::PyType>() { return Ok(None); }
    let dictionary = markup.getattr("__dict__")?;
    let descriptor = dictionary.call_method1("get", ("__new__",))?;
    if descriptor.get_type().as_type_ptr() != &raw mut PyStaticMethod_Type { return Ok(None); }
    let function = descriptor.getattr("__func__")?;
    if let Some(original) = MARKUP_CONSTRUCTOR.get(py) {
        return Ok((markup.is(original.class.bind(py)) && original.function.matches(&function)?)
            .then_some(function));
    }
    if !function.is_exact_instance_of::<pyo3::types::PyFunction>() { return Ok(None); }
    let module = py.import("markupsafe")?;
    if !function.getattr("__globals__")?.is(&module.dict()) { return Ok(None); }
    let name = markup.getattr("__name__")?;
    let module_name = markup.getattr("__module__")?;
    let code = function.getattr("__code__")?;
    let code_name = code.getattr("co_name")?;
    let filename = code.getattr("co_filename")?;
    let Some(module_file) = module.dict().get_item("__file__")? else { return Ok(None); };
    if !name.is_exact_instance_of::<pyo3::types::PyString>() || !name.eq("Markup")?
        || !module_name.is_exact_instance_of::<pyo3::types::PyString>() || !module_name.eq("markupsafe")?
        || !code_name.is_exact_instance_of::<pyo3::types::PyString>() || !code_name.eq("__new__")?
        || !filename.is_exact_instance_of::<pyo3::types::PyString>()
        || !module_file.is_exact_instance_of::<pyo3::types::PyString>() || !filename.eq(&module_file)? {
        return Ok(None);
    }
    let bases = markup.getattr("__bases__")?.cast_into::<PyTuple>()?;
    if bases.len() != 1 || !bases.get_item(0)?.is(&py.get_type::<pyo3::types::PyString>()) { return Ok(None); }
    let original = MarkupConstructor { class: markup.clone().unbind(),
        function: StandardFunction::capture_value(&module, "__new__", &function)? };
    let _ = MARKUP_CONSTRUCTOR.set(py, original);
    Ok(Some(function))
}

/// Implement the original Markup constructor for a package image while
/// retaining dynamic global lookups and the actual HTML/string protocols.
fn markup_default_image<'py>(markup: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>)
    -> PyResult<Option<Bound<'py, PyAny>>> {
    if !docxtpl_core::has_owned_callback_image_scope()
        || !crate::python_bridge::concrete_default_image(value)?
        || !crate::python_bridge::default_image_protocol(value, true)? {
        return Ok(None);
    }
    let Some(function) = standard_markup_constructor(markup)? else { return Ok(None); };
    let py = value.py();
    let args = PyTuple::new(py, [markup, value])?;
    let arguments = crate::python_function::bind_fixed(&function, &args, &PyDict::new(py))?;
    let encoding = &arguments[2];
    let errors = &arguments[3];
    let scope = FrameGlobals::of_function(&function)?;
    let html = global(&scope, "hasattr")?.call1((value, "__html__"))?.is_truthy()?;
    let converted = if html {
        match crate::python_operators::native_default_image_string(value, true)? {
            Some(text) => text,
            None => value.call_method0("__html__")?,
        }
    } else { value.clone() };
    let super_class = global(&scope, "super")?;
    let default_super = super_class.is(&py.get_type::<pyo3::types::PySuper>());
    let parent = if py.version_info().minor <= 11 && !default_super {
        super_class.call0()?
    } else {
        let closure = function.getattr("__closure__")?.cast_into::<PyTuple>()?;
        let class = match closure.get_item(0)?.getattr("cell_contents") {
            Ok(class) => class,
            Err(error) if error.is_instance_of::<pyo3::exceptions::PyValueError>(py) => {
                if py.version_info().minor <= 11 {
                    return Err(pyo3::exceptions::PyRuntimeError::new_err("super(): empty __class__ cell"));
                }
                return Err(pyo3::exceptions::PyNameError::new_err(
                    "cannot access free variable '__class__' where it is not associated with a value in enclosing scope"));
            }
            Err(error) => return Err(error),
        };
        if default_super && !class.is_instance_of::<pyo3::types::PyType>() {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                "super(): __class__ is not a type ({})", class.get_type().name()?)));
        }
        if default_super { super_class.call1((class, markup))? }
        else { super_class.call0()? }
    };
    let new = parent.getattr("__new__")?;
    let converted = if !html && encoding.is_none()
        && new.is(&py.get_type::<pyo3::types::PyString>().getattr("__new__")?) {
        crate::python_operators::native_default_image_string(&converted, false)?.unwrap_or(converted)
    } else { converted };
    let result = if encoding.is_none() { new.call1((markup, converted))? }
        else { new.call1((markup, converted, encoding, errors))? };
    // type.__call__ reads tp_init after __new__, so changes made by a stream
    // callback during insertion must remain observable here too.
    if unsafe { pyo3::ffi::PyObject_TypeCheck(result.as_ptr(), markup.as_ptr().cast()) } != 0 {
        let args = PyTuple::new(py, [value])?;
        let init = unsafe { (*pyo3::ffi::Py_TYPE(result.as_ptr())).tp_init };
        if let Some(init) = init {
            if unsafe { init(result.as_ptr(), args.as_ptr(), std::ptr::null_mut()) } < 0 {
                return Err(PyErr::fetch(py));
            }
        }
    }
    Ok(Some(result))
}

pub(crate) fn initialize(py: Python<'_>) -> PyResult<()> {
    FUNCTIONS.get_or_try_init(py, || {
        let module = py.import("jinja2.filters")?;
        let mut functions = Vec::new();
        for name in NAMES {
            if module
                .getattr(*name)?
                .is_exact_instance_of::<pyo3::types::PyFunction>()
            {
                functions.push((*name, StandardFunction::capture(&module, name)?));
            }
        }
        Ok::<_, PyErr>(functions)
    })?;
    VARIANT_WRAPPERS.get_or_try_init(py, || {
        let module = py.import("jinja2.filters")?;
        let mut wrappers = Vec::new();
        for (filter, _) in SYNC_VARIANTS {
            let wrapper = module.getattr(*filter)?;
            if wrapper.is_exact_instance_of::<pyo3::types::PyFunction>() {
                let code = wrapper.getattr("__code__")?;
                wrappers.push((*filter, wrapper.unbind(), code.unbind()));
            }
        }
        Ok::<_, PyErr>(wrappers)
    })?;
    ASYNC_FUNCTIONS.get_or_try_init(py, || {
        let module = py.import("jinja2.filters")?;
        let mut functions = Vec::new();
        for name in ASYNC_PORTED {
            let wrapper = module.getattr(*name)?;
            let closure = wrapper.getattr_opt("__closure__")?;
            let Some(closure) = closure.filter(|closure| !closure.is_none()) else { continue; };
            for cell in closure.try_iter()? {
                let Ok(contents) = cell?.getattr("cell_contents") else { continue; };
                if contents.is_exact_instance_of::<pyo3::types::PyFunction>()
                    && contents.getattr("__name__")?.eq(*name)? {
                    let code = contents.getattr("__code__")?.unbind();
                    functions.push((*name, contents.unbind(), code));
                }
            }
        }
        Ok::<_, PyErr>(functions)
    })?;
    SYNC_FUNCTIONS.get_or_try_init(py, || {
        let module = py.import("jinja2.filters")?;
        let mut functions = Vec::new();
        for (filter, name) in SYNC_VARIANTS {
            if module.getattr(*name)?.is_exact_instance_of::<pyo3::types::PyFunction>() {
                functions.push((*filter, StandardFunction::capture(&module, name)?));
            }
        }
        Ok::<_, PyErr>(functions)
    })?;
    ENVIRONMENT_FUNCTIONS.get_or_try_init(py, || {
        let module = py.import("jinja2.environment")?;
        let class = module.getattr("Environment")?;
        let mut functions = Vec::new();
        for name in ENVIRONMENT_METHODS {
            let function = class.getattr(*name)?;
            if function.is_exact_instance_of::<pyo3::types::PyFunction>() {
                functions.push((*name, StandardFunction::capture_value(&module, name, &function)?));
            }
        }
        Ok::<_, PyErr>(functions)
    })?;
    HELPER_FUNCTIONS.get_or_try_init(py, || {
        let module = py.import("jinja2.filters")?;
        let utils = py.import("jinja2.utils")?;
        let mut functions = Vec::new();
        for name in HELPERS {
            // url_quote and pformat are jinja2.utils functions imported by filters.
            let owner = if matches!(*name, "url_quote" | "pformat" | "urlize") { &utils } else { &module };
            if owner.getattr(*name)?.is_exact_instance_of::<pyo3::types::PyFunction>() {
                functions.push((*name, StandardFunction::capture(owner, name)?));
            }
        }
        Ok::<_, PyErr>(functions)
    })?;
    SOFT_STR.get_or_try_init(py, || {
        let module = py.import("markupsafe")?;
        if module
            .getattr("soft_str")?
            .is_exact_instance_of::<pyo3::types::PyFunction>()
        {
            StandardFunction::capture(&module, "soft_str").map(Some)
        } else {
            Ok(None)
        }
    })?;
    WORD_RE.get_or_try_init(py, || {
        Ok::<_, PyErr>(py.import("jinja2.filters")?.getattr("_word_re")?.unbind())
    })?;
    CAST.get_or_try_init(py, || {
        let module = py.import("typing")?;
        if module
            .getattr("cast")?
            .is_exact_instance_of::<pyo3::types::PyFunction>()
        {
            StandardFunction::capture(&module, "cast").map(Some)
        } else {
            Ok(None)
        }
    })?;
    ESCAPE.get_or_try_init(py, || {
        let module = py.import("markupsafe")?;
        // Jinja retains this import independently of the public MarkupSafe
        // alias. A public override must not become the original implementation
        // merely because it was installed before the extension loaded.
        let Some(runtime) = crate::find_loaded_module(py, pyo3::intern!(py, "jinja2.runtime"))? else {
            return Ok(None);
        };
        let Ok(runtime) = runtime.bind(py).cast::<pyo3::types::PyModule>() else { return Ok(None); };
        let Some(function) = runtime.dict().get_item("escape")? else { return Ok(None); };
        if !function.is_exact_instance_of::<pyo3::types::PyFunction>() { return Ok(None); }
        if !escape_implementation_shape(&function, false)? { return Ok(None); }
        let code = function.getattr("__code__")?;
        let Some(filename) = module.dict().get_item("__file__")? else { return Ok(None); };
        if filename.is_exact_instance_of::<pyo3::types::PyString>()
            && function.getattr("__globals__")?.is(&module.dict())
            && code.getattr("co_name")?.eq("escape")?
            && code.getattr("co_filename")?.eq(filename)? {
            StandardFunction::capture_value(&module, "escape", &function).map(Some)
        } else { Ok(None) }
    })?;
    ESCAPE_INNER.get_or_try_init(py, || {
        // The public global may already hold a user callback. Capture the
        // loaded implementation instead of extending that callback's life.
        let module = py.import("markupsafe")?;
        if let Some(current) = module.dict().get_item("_escape_inner")? {
            if looks_like_speedups_inner(&current)? {
                return Ok::<_, PyErr>(Some(current.unbind()));
            }
        }
        loaded_speedups_inner(py)
    })?;
    ESCAPE_INNER_PY.get_or_try_init(py, || {
        let module = py.import("markupsafe")?;
        let Some(inner) = module.dict().get_item("_escape_inner")? else { return Ok(None); };
        if inner.is_exact_instance_of::<pyo3::types::PyFunction>() {
            if !escape_implementation_shape(&inner, true)? { return Ok(None); }
            let module_name = inner.getattr("__module__")?;
            let function_name = inner.getattr("__name__")?;
            if !module_name.is_exact_instance_of::<pyo3::types::PyString>()
                || !function_name.is_exact_instance_of::<pyo3::types::PyString>()
                || !module_name.eq("markupsafe._native")? || !function_name.eq("_escape_inner")? {
                return Ok(None);
            }
            let Some(implementation) = crate::find_loaded_module(py, pyo3::intern!(py, "markupsafe._native"))? else {
                return Ok(None);
            };
            let Ok(implementation) = implementation.bind(py).cast::<PyModule>() else { return Ok(None); };
            if implementation.dict().get_item("_escape_inner")?.is_some_and(|value| inner.is(&value)) {
                StandardFunction::capture_value(&implementation, "_escape_inner", &inner).map(Some)
            } else { Ok(None) }
        } else { Ok(None) }
    })?;
    METHOD_TYPE.get_or_try_init(py, || {
        // Derive the interpreter's method type without trusting the mutable
        // types.MethodType attribute or executing the function being bound.
        for (module_name, name) in [("markupsafe", "escape"), ("typing", "cast"),
            ("jinja2.filters", "do_replace"), ("jinja2.filters", "do_forceescape")] {
            let module_name = pyo3::types::PyString::new(py, module_name);
            let Some(module) = crate::find_loaded_module(py, &module_name)?
                else { continue; };
            let Ok(module) = module.bind(py).cast::<pyo3::types::PyModule>() else { continue; };
            let Some(function) = module.dict().get_item(name)? else { continue; };
            if function.is_exact_instance_of::<pyo3::types::PyFunction>() {
                let receiver = pyo3::types::PyBool::new(py, true).to_owned();
                let method = function.call_method1("__get__", (&receiver, receiver.get_type()))?;
                return Ok(Some(method.get_type().into_any().unbind()));
            }
        }
        Ok::<_, PyErr>(None)
    })?;
    CLASSMETHOD_TYPE.get_or_try_init(py, || {
        let descriptor = unsafe { Bound::from_owned_ptr_or_err(py, PyClassMethod_New(py.None().as_ptr()))? };
        Ok::<_, PyErr>(descriptor.get_type().into_any().unbind())
    })?;
    markup_standard(py, "replace")?;
    markup_standard(py, "escape")?;
    Ok(())
}

fn global<'py>(scope: &FrameGlobals<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    frame_global(scope, &pyo3::types::PyString::new(scope.py(), name))
}

fn loaded_speedups_inner(py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
    use pyo3::types::PyModule;
    let Some(module) = crate::find_loaded_module(py, pyo3::intern!(py, "markupsafe._speedups"))? else {
        return Ok(None);
    };
    let Ok(module) = module.bind(py).cast::<PyModule>() else { return Ok(None); };
    let Some(inner) = module.dict().get_item("_escape_inner")? else { return Ok(None); };
    if looks_like_speedups_inner(&inner)? {
        Ok(Some(inner.unbind()))
    } else { Ok(None) }
}

fn looks_like_speedups_inner(inner: &Bound<'_, PyAny>) -> PyResult<bool> {
    if !inner.is_exact_instance_of::<pyo3::types::PyCFunction>() { return Ok(false); }
    let module = inner.getattr("__module__")?;
    let name = inner.getattr("__name__")?;
    Ok(module.is_exact_instance_of::<pyo3::types::PyString>()
        && name.is_exact_instance_of::<pyo3::types::PyString>()
        && module.eq("markupsafe._speedups")? && name.eq("_escape_inner")?)
}

fn escape_inner<'py>(inner: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    let standard_python = match ESCAPE_INNER_PY.get(py).and_then(Option::as_ref) {
        Some(original) => original.matches(inner)?,
        None => false,
    };
    if standard_python {
        // The pure-Python distribution dispatches replace on the result of
        // each preceding call, so user string subclasses remain observable.
        let mut result = value.clone();
        for (from, to) in [("&", "&amp;"), (">", "&gt;"), ("<", "&lt;"),
            ("'", "&#39;"), ("\"", "&#34;")] {
            result = result.call_method1("replace", (from, to))?;
        }
        return Ok(result);
    }
    if let Some(original) = ESCAPE_INNER_PY.get(py).and_then(Option::as_ref) {
        if original.has_replaced_code(inner)? {
            return inner.call1((value,));
        }
    }
    // A real pure-Python implementation can outlive its module's sys.modules
    // entry. Do not import that module during initialization or run an
    // unverified copy of the standard algorithm through a Python callback.
    if inner.is_exact_instance_of::<pyo3::types::PyFunction>() {
        let globals = inner.getattr("__globals__")?.cast_into::<PyDict>()?;
        if let Some(name) = globals.get_item("__name__")? {
            let code_name = inner.getattr("__code__")?.getattr("co_name")?;
            if escape_implementation_shape(inner, true)?
                && name.is_exact_instance_of::<pyo3::types::PyString>()
                && code_name.is_exact_instance_of::<pyo3::types::PyString>()
                && name.eq("markupsafe._native")? && code_name.eq("_escape_inner")? {
                return Err(crate::python_bridge::unsupported(py, "cannot verify pure Python escape implementation provenance"));
            }
        }
    }
    let looks_native = looks_like_speedups_inner(inner)?;
    let original = ESCAPE_INNER.get(py).and_then(Option::as_ref);
    let native = if let Some(original) = original {
        looks_native && original.bind(py).is(inner)
    } else if looks_native {
        loaded_speedups_inner(py)?.is_some_and(|original| original.bind(py).is(inner))
    } else { false };
    if looks_native && !native && original.is_none() {
        return Err(crate::python_bridge::unsupported(py, "cannot verify native escape implementation provenance"));
    }
    if !native { return inner.call1((value,)); }
    if !value.is_instance_of::<pyo3::types::PyString>() {
        // Delegate only the invalid-input error to the C callable; no escaping
        // algorithm runs. CPython's message differs across versions/call sites.
        return inner.call1((value,));
    }
    crate::python_operators::escape_string(value)
}

fn forceescape<'py>(scope: &FrameGlobals<'py>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = scope.py();
    let value = if global(scope, "hasattr")?.call1((value, "__html__"))?.is_truthy()? {
        let cast = global(scope, "t")?.getattr("cast")?;
        let standard = match CAST.get(py).and_then(Option::as_ref) {
            Some(original) => original.matches(&cast)?,
            None => false,
        };
        let input = if standard { value.clone() } else { cast.call1(("HasHTML", value))? };
        input.call_method0("__html__")?
    } else { value.clone() };
    // Preserve Python's lookup order: escape is captured before str is called.
    let escape = global(scope, "escape")?;
    let text = global(scope, "str")?.call1((value,))?;
    escape_value(&escape, &text)
}

pub(crate) fn escape_value<'py>(escape: &Bound<'py, PyAny>, text: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = text.py();
    let standard = match ESCAPE.get(py).and_then(Option::as_ref) {
        Some(original) => original.matches(escape)?,
        None => false,
    };
    if !standard {
        if let Some(original) = ESCAPE.get(py).and_then(Option::as_ref) {
            if original.has_replaced_code(escape)? {
                return escape.call1((text,));
            }
        }
        if ESCAPE.get(py).and_then(Option::as_ref).is_none()
            && escape.is_exact_instance_of::<pyo3::types::PyFunction>()
            && escape_implementation_shape(escape, false)?
            && escape.getattr("__code__")?.getattr("co_name")?.eq("escape")? {
            let module = escape.getattr("__module__")?;
            if module.is_exact_instance_of::<pyo3::types::PyString>() && module.eq("markupsafe")? {
                return Err(crate::python_bridge::unsupported(py, "cannot verify standard escape implementation provenance"));
            }
        }
        return escape.call1((text,));
    }
    let scope = FrameGlobals::of_function(escape)?;
    let exact = global(&scope, "type")?.call1((&text,))?.is(&global(&scope, "str")?);
    if exact {
        let markup = global(&scope, "Markup")?;
        let inner = global(&scope, "_escape_inner")?;
        let escaped = escape_inner(&inner, text)?;
        drop(inner);
        return markup.call1((escaped,));
    }
    if global(&scope, "hasattr")?.call1((&text, "__html__"))?.is_truthy()? {
        let markup = global(&scope, "Markup")?;
        return markup.call1((text.call_method0("__html__")?,));
    }
    let markup = global(&scope, "Markup")?;
    let inner = global(&scope, "_escape_inner")?;
    let text = global(&scope, "str")?.call1((&text,))?;
    let escaped = escape_inner(&inner, &text)?;
    drop(inner);
    drop(text);
    markup.call1((escaped,))
}

pub(crate) fn is_standard_cast(function: &Bound<'_, PyAny>) -> PyResult<bool> {
    match CAST.get(function.py()).and_then(Option::as_ref) {
        Some(original) => original.matches(function),
        None => Ok(false),
    }
}

fn standard_bound_method<'py>(method: &Bound<'py, PyAny>, original: Option<&StandardFunction>)
    -> PyResult<Option<(Bound<'py, PyAny>, Bound<'py, PyAny>)>> {
    let py = method.py();
    let Some(original) = original else { return Ok(None); };
    if !METHOD_TYPE.get(py).and_then(Option::as_ref)
        .is_some_and(|method_type| method.get_type().is(method_type.bind(py))) {
        return Ok(None);
    }
    let function = method.getattr("__func__")?;
    if !original.matches(&function)? { return Ok(None); }
    Ok(Some((function, method.getattr("__self__")?)))
}

fn markup_escape<'py>(method: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    let Some((function, class)) = standard_bound_method(method, markup_standard(py, "escape")?)?
        else { return method.call1((value,)); };
    let scope = FrameGlobals::of_function(&function)?;
    let escape = global(&scope, "escape")?;
    let result = escape_value(&escape, value)?;
    drop(escape);
    if result.getattr("__class__")?.is(&class) { Ok(result) }
    else { class.call1((result,)) }
}

fn markup_replace<'py>(method: &Bound<'py, PyAny>, old: &Bound<'py, PyAny>, new: &Bound<'py, PyAny>,
    count: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = method.py();
    let Some((function, receiver)) = standard_bound_method(method, markup_standard(py, "replace")?)?
        else { return method.call1((old, new, count)); };
    let code = function.getattr("__code__")?;
    let names = code.getattr("co_freevars")?.cast_into::<PyTuple>()?;
    let Some(index) = names.iter().position(|name| name.eq("__class__").unwrap_or(false))
        else { return method.call1((old, new, count)); };
    let closure = function.getattr("__closure__")?.cast_into::<PyTuple>()?;
    let class_cell = closure.get_item(index)?;
    // The original bytecode captures __class__, then super().replace, before
    // resolving self.escape; these lookups may invoke user protocols.
    let result_class = receiver.getattr("__class__")?;
    let scope = FrameGlobals::of_function(&function)?;
    let super_function = global(&scope, "super")?;
    let default_super = super_function.is(&py.get_type::<pyo3::types::PySuper>());
    let parent = if py.version_info().minor <= 11 && !default_super {
        // CPython 3.11 calls the replacement with no closure access.
        super_function.call0()?
    } else {
        let class_cell = match class_cell.getattr("cell_contents") {
            Ok(class) => class,
            Err(error) if error.is_instance_of::<pyo3::exceptions::PyValueError>(py) => {
                if py.version_info().minor <= 11 {
                    return Err(pyo3::exceptions::PyRuntimeError::new_err("super(): empty __class__ cell"));
                }
                return Err(pyo3::exceptions::PyNameError::new_err(
                    "cannot access free variable '__class__' where it is not associated with a value in enclosing scope"));
            }
            Err(error) => return Err(error),
        };
        if default_super { super_function.call1((class_cell, &receiver))? }
        else { super_function.call0()? }
    };
    drop(super_function);
    let replace = parent.getattr("replace")?;
    drop(parent);
    let escape = receiver.getattr("escape")?;
    let escaped = markup_escape(&escape, new)?;
    drop(escape);
    let result = replace.call1((old, escaped, count))?;
    drop(replace);
    result_class.call1((result,))
}

fn replace_filter<'py>(scope: &FrameGlobals<'py>, args: &[Bound<'py, PyAny>]) -> PyResult<Bound<'py, PyAny>> {
    let py = scope.py();
    let count = if args[4].is_none() { (-1i32).into_pyobject(py)?.into_any() } else { args[4].clone() };
    if !args[0].getattr("autoescape")?.is_truthy()? {
        let value = global(scope, "str")?.call1((&args[1],))?;
        let replace = value.getattr("replace")?;
        let old = global(scope, "str")?.call1((&args[2],))?;
        let new = global(scope, "str")?.call1((&args[3],))?;
        return replace.call1((old, new, count));
    }
    let old_html = global(scope, "hasattr")?.call1((&args[2], "__html__"))?.is_truthy()?;
    let escape = if old_html { true } else {
        global(scope, "hasattr")?.call1((&args[3], "__html__"))?.is_truthy()?
            && !global(scope, "hasattr")?.call1((&args[1], "__html__"))?.is_truthy()?
    };
    let value = if escape {
        let callback = global(scope, "escape")?;
        escape_value(&callback, &args[1])?
    } else { soft_str(scope, &args[1])? };
    let replace = value.getattr("replace")?;
    let old = soft_str(scope, &args[2])?;
    let new = soft_str(scope, &args[3])?;
    markup_replace(&replace, &old, &new, &count)
}

#[pyclass(module = "docxtpl_native._native")]
struct IndentIterator {
    iterator: Option<Py<PyAny>>,
    indentation: Option<Py<PyAny>>,
    current: Option<Py<PyAny>>,
    // The filter and adapter share one Python reference, so callbacks do not
    // observe an additional Python reference while the generator runs.
    lines: Option<std::sync::Arc<Py<PyAny>>>,
    failed: bool,
}

#[pymethods]
impl IndentIterator {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }

    fn __next__(&mut self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        let (Some(iterator), Some(indentation)) = (&self.iterator, &self.indentation) else { return Ok(None); };
        let pointer = unsafe { pyo3::ffi::PyIter_Next(iterator.bind(py).as_ptr()) };
        if pointer.is_null() {
            if PyErr::occurred(py) {
                self.failed = true;
                return Err(PyErr::fetch(py));
            }
            // A completed Python generator releases its iterator stack entry
            // before the last line local. Both can run observable finalizers.
            drop(self.iterator.take());
            drop(self.current.take());
            return Ok(None);
        }
        let item = unsafe { Bound::from_owned_ptr_or_err(py, pointer)? };
        let previous = self.current.replace(item.clone().unbind());
        drop(previous);
        let result = (|| -> PyResult<Bound<'_, PyAny>> {
            if item.is_truthy()? {
                crate::python_operators::python_operator(py, "add", indentation.bind(py), &item)
            } else { Ok(item) }
        })();
        result.map(|value| Some(value.unbind())).map_err(|error| {
            self.failed = true;
            crate::python_errors::escaped_iteration(py, error, crate::python_errors::Boundary::Generator)
        })
    }

    fn __traverse__(&self, visit: pyo3::gc::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        visit.call(&self.iterator)?;
        visit.call(&self.indentation)?;
        visit.call(&self.current)?;
        if let Some(lines) = &self.lines { visit.call(lines.as_ref())?; }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.iterator.take();
        self.indentation.take();
        self.current.take();
        self.lines.take();
    }
}

pub(crate) fn register(module: &Bound<'_, pyo3::types::PyModule>) -> PyResult<()> {
    module.add_class::<IndentIterator>()?;
    module.add_class::<NativeSortKey>()?;
    module.add_class::<NativeFilterIterator>()?;
    module.add_class::<NativeAsyncVariant>()?;
    module.add_class::<NativeAsyncIterating>()
}

fn helpers_standard(scope: &FrameGlobals<'_>) -> PyResult<bool> {
    let py = scope_py(scope)?;
    let Some(helpers) = HELPER_FUNCTIONS.get(py) else { return Ok(false); };
    if helpers.len() != HELPERS.len() { return Ok(false); }
    for (name, original) in helpers {
        if !original.matches(&global(scope, name)?)? { return Ok(false); }
    }
    Ok(true)
}

/// Jinja's `url_quote`, with its own module's globals.
fn url_quote<'py>(scope: &FrameGlobals<'py>, value: &Bound<'py, PyAny>, for_qs: bool) -> PyResult<Bound<'py, PyAny>> {
    let utils = FrameGlobals::of_function(&global(scope, "url_quote")?)?;
    let isinstance = global(&utils, "isinstance")?;
    let mut value = value.clone();
    if !isinstance.call1((&value, global(&utils, "bytes")?))?.is_truthy()? {
        if !isinstance.call1((&value, global(&utils, "str")?))?.is_truthy()? {
            value = global(&utils, "str")?.call1((&value,))?;
        }
        value = value.call_method1("encode", ("utf-8",))?;
    }
    let safe = pyo3::types::PyBytes::new(value.py(), if for_qs { b"" } else { b"/" });
    let quoted = global(&utils, "quote_from_bytes")?.call1((&value, safe))?;
    if for_qs { quoted.call_method1("replace", ("%20", "+")) } else { Ok(quoted) }
}

/// `for k, v in items`: the unpacking errors of CPython.
fn unpack_pair<'py>(item: &Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let iterator = match item.try_iter() {
        Ok(iterator) => iterator,
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyTypeError>(item.py()) => {
            return Err(pyo3::exceptions::PyTypeError::new_err(format!(
                "cannot unpack non-iterable {} object", item.get_type().name()?)));
        }
        Err(error) => return Err(error),
    };
    let mut values = Vec::with_capacity(2);
    for value in iterator {
        values.push(value?);
        if values.len() > 2 {
            return Err(pyo3::exceptions::PyValueError::new_err("too many values to unpack (expected 2)"));
        }
    }
    if values.len() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "not enough values to unpack (expected 2, got {})", values.len())));
    }
    let second = values.pop().unwrap();
    Ok((values.pop().unwrap(), second))
}

/// `do_urlencode`, `do_wordwrap`, `do_pprint`, `do_random` and `do_attr` with
/// the original helpers and the module's globals.
fn text_filter<'py>(name: &str, scope: &FrameGlobals<'py>, args: &[Bound<'py, PyAny>]) -> PyResult<Bound<'py, PyAny>> {
    let py = scope_py(scope)?;
    match name {
        "do_urlencode" => {
            let value = &args[0];
            let isinstance = global(scope, "isinstance")?;
            if isinstance.call1((value, global(scope, "str")?))?.is_truthy()?
                || !isinstance.call1((value, global(scope, "abc")?.getattr("Iterable")?))?.is_truthy()? {
                return url_quote(scope, value, false);
            }
            let items = if isinstance.call1((value, global(scope, "dict")?))?.is_truthy()? {
                value.call_method0("items")?
            } else { value.clone() };
            let mut parts = Vec::new();
            for item in items.try_iter()? {
                let (key, item) = unpack_pair(&item?)?;
                let key = url_quote(scope, &key, true)?;
                let item = url_quote(scope, &item, true)?;
                parts.push(format!("{}={}", key.str()?, item.str()?));
            }
            Ok(pyo3::types::PyString::new(py, &parts.join("&")).into_any())
        }
        "do_wordwrap" => {
            let (environment, text, width, break_long, wrapstring, break_hyphens) =
                (&args[0], &args[1], &args[2], &args[3], &args[4], &args[5]);
            let textwrap = py.import("textwrap")?;
            let wrapstring = if wrapstring.is_none() { environment.getattr("newline_sequence")? } else { wrapstring.clone() };
            let keywords = PyDict::new(py);
            keywords.set_item("width", width)?;
            keywords.set_item("expand_tabs", false)?;
            keywords.set_item("replace_whitespace", false)?;
            keywords.set_item("break_long_words", break_long)?;
            keywords.set_item("break_on_hyphens", break_hyphens)?;
            let paragraphs = pyo3::types::PyList::empty(py);
            for line in text.call_method0("splitlines")?.try_iter()? {
                let lines = textwrap.getattr("wrap")?.call((line?,), Some(&keywords))?;
                paragraphs.append(wrapstring.call_method1("join", (lines,))?)?;
            }
            wrapstring.call_method1("join", (paragraphs,))
        }
        "do_pprint" => py.import("pprint")?.getattr("pformat")?.call1((&args[0],)),
        "do_random" => {
            let (context, sequence) = (&args[0], &args[1]);
            match global(scope, "random")?.getattr("choice")?.call1((sequence,)) {
                Ok(value) => Ok(value),
                Err(error) if error.is_instance_of::<pyo3::exceptions::PyIndexError>(py) => context
                    .getattr("environment")?.getattr("undefined")?.call1(("No random item, sequence was empty.",)),
                Err(error) => Err(error),
            }
        }
        _ => {
            let (environment, object, attribute) = (&args[0], &args[1], &args[2]);
            if let Err(error) = global(scope, "getattr_static")?.call1((object, attribute)) {
                if !error.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) { return Err(error); }
                if !global(scope, "hasattr")?.call1((object, attribute))?.is_truthy()? {
                    let keywords = PyDict::new(py);
                    keywords.set_item("obj", object)?;
                    keywords.set_item("name", attribute)?;
                    return environment.getattr("undefined")?.call((), Some(&keywords));
                }
            }
            let getattr = environment.getattr("getattr")?;
            let binding = crate::python_function::binding(py)?;
            crate::python_lookup::dispatch_lookup(&getattr, object, attribute, true, &binding)
        }
    }
}

/// An `async_variant` filter: its wrapper picks the async body when the
/// environment is async (kept in Python) and the sync body otherwise, which
/// runs here when the wrapped function is the original.
fn async_variant_filter<'py>(name: &str, wrapper: &Bound<'py, PyAny>, args: &Bound<'py, PyTuple>,
    kwargs: &Bound<'py, PyDict>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = wrapper.py();
    let Some((_, sync)) = SYNC_FUNCTIONS.get(py).and_then(|functions| functions.iter().find(|(filter, _)| *filter == name))
        else { return Ok(None); };
    let normal = wrapper.getattr("__wrapped__")?;
    if !sync.matches(&normal)? { return Ok(None); }
    // is_async(args): the environment, or the (eval) context's environment.
    // A sync function without a pass decorator got an eval context, which the
    // wrapper drops before the call.
    let pass_arg = match normal.getattr_opt(pyo3::intern!(py, "jinja_pass_arg"))? {
        Some(pass_arg) => Some(pass_arg.getattr("name")?.extract::<String>()?),
        None => None,
    };
    let first = args.get_item(0)?;
    let environment = if pass_arg.as_deref() == Some("environment") { first.clone() } else { first.getattr("environment")? };
    let asynchronous = environment.getattr("is_async")?.is_truthy()?;
    let args = if pass_arg.is_none() { args.get_slice(1, args.len()) } else { args.clone() };
    if asynchronous { return async_variant(name, wrapper, &normal, &args, kwargs); }
    // Some(select mode) for select/reject(attr), None for map.
    let iterating = match name {
        "do_select" => Some(Some((false, false))), "do_reject" => Some(Some((false, true))),
        "do_selectattr" => Some(Some((true, false))), "do_rejectattr" => Some(Some((true, true))),
        "do_map" => Some(None), _ => None,
    };
    if let Some(mode) = iterating {
        // (context, value, *args, **kwargs): an unusual binding runs the body.
        let scope = FrameGlobals::of_function(&normal)?;
        if args.len() < 2 || kwargs.contains("context")? || kwargs.contains("value")? || !helpers_standard(&scope)? {
            return normal.call(&args, Some(kwargs)).map(Some);
        }
        let (context, value) = (args.get_item(0)?.unbind(), args.get_item(1)?.unbind());
        let rest = args.get_slice(2, args.len()).unbind();
        let keywords = kwargs.copy()?.unbind();
        return Ok(Some(match mode {
            Some((attribute, reject)) => filter_generator(py, &scope, FilterIteration::Select { context, value, args: rest,
                kwargs: keywords, attribute, reject, started: None }, "select_or_reject")?,
            None => filter_generator(py, &scope, FilterIteration::Map { context, value, args: rest, kwargs: keywords,
                started: None }, "sync_do_map")?,
        }));
    }
    let bound = crate::python_function::bind_fixed(&normal, &args, kwargs)?;
    let scope = FrameGlobals::of_function(&normal)?;
    if !helpers_standard(&scope)? { return normal.call1(PyTuple::new(py, &bound)?).map(Some); }
    match name {
        "do_first" => {
            let (environment, sequence) = (&bound[0], &bound[1]);
            let iterator = global(&scope, "iter")?.call1((sequence,))?;
            match global(&scope, "next")?.call1((iterator,)) {
                Ok(value) => Ok(Some(value)),
                Err(error) if error.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => environment
                    .getattr("undefined")?.call1(("No first item, sequence was empty.",)).map(Some),
                Err(error) => Err(error),
            }
        }
        "do_list" => global(&scope, "list")?.call1((&bound[0],)).map(Some),
        "do_slice" => filter_generator(py, &scope, FilterIteration::Slice { value: bound[0].clone().unbind(),
            slices: bound[1].clone().unbind(), fill_with: bound[2].clone().unbind(), prepared: None }, "sync_do_slice").map(Some),
        "do_unique" => filter_generator(py, &scope, FilterIteration::Unique { environment: bound[0].clone().unbind(),
            value: bound[1].clone().unbind(), case_sensitive: bound[2].clone().unbind(), attribute: bound[3].clone().unbind(),
            started: None }, "sync_do_unique").map(Some),
        "do_groupby" => groupby_filter(&scope, &bound[0], &bound[1], &bound[2], &bound[3], &bound[4]).map(Some),
        _ => {
            let (environment, iterable, attribute, start) = (&bound[0], &bound[1], &bound[2], &bound[3]);
            let iterable = if attribute.is_none() { iterable.clone() } else {
                let parts = attribute_parts(&scope, attribute)?;
                let key = Py::new(py, NativeSortKey { kind: SortKeyKind::Single, environment: Some(environment.clone().unbind()),
                    parts: vec![parts], lower: false, default: None, globals: global(&scope, "ignore_case")?.unbind(),
                    binding: crate::python_function::binding(py)?.unbind() })?;
                global(&scope, "map")?.call1((key, iterable))?
            };
            global(&scope, "sum")?.call1((iterable, start)).map(Some)
        }
    }
}

/// The async body of an async_variant filter, as a coroutine named like
/// Jinja's: a sync iterable is iterated as `auto_aiter` does and the body
/// runs here; an async iterable awaits the reference body. None runs the
/// wrapper when the body or its helpers were replaced.
fn async_variant<'py>(name: &str, wrapper: &Bound<'py, PyAny>, normal: &Bound<'py, PyAny>, args: &Bound<'py, PyTuple>,
    kwargs: &Bound<'py, PyDict>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = wrapper.py();
    let Some((name, function, code)) = ASYNC_FUNCTIONS.get(py)
        .and_then(|functions| functions.iter().find(|(filter, _, _)| *filter == name)) else { return Ok(None); };
    let function = function.bind(py);
    // The wrapper's closure still holds the original async body.
    let Some(closure) = wrapper.getattr_opt("__closure__")?.filter(|closure| !closure.is_none()) else { return Ok(None); };
    let mut held = false;
    for cell in closure.try_iter()? {
        if cell?.getattr("cell_contents").is_ok_and(|contents| contents.is(function)) { held = true; }
    }
    if !held || !function.getattr("__code__")?.is(code.bind(py)) { return Ok(None); }
    let scope = FrameGlobals::of_function(function)?;
    if !helpers_standard(&scope)? || !async_helpers_standard(&scope)? { return Ok(None); }
    // do_slice and do_unique call the module's sync body by name.
    if matches!(*name, "do_slice" | "do_unique") {
        let sync_name = if *name == "do_slice" { "sync_do_slice" } else { "sync_do_unique" };
        let standard = SYNC_FUNCTIONS.get(py).and_then(|functions| functions.iter().find(|(filter, _)| filter == name))
            .map(|(_, original)| original.matches(&global(&scope, sync_name)?)).transpose()?;
        if standard != Some(true) { return Ok(None); }
    }
    let _ = normal;
    let iterating = match *name {
        "do_select" => Some(Some((false, false))), "do_reject" => Some(Some((false, true))),
        "do_selectattr" => Some(Some((true, false))), "do_rejectattr" => Some(Some((true, true))),
        "do_map" => Some(None), _ => None,
    };
    if let Some(mode) = iterating {
        // (context, value, *args, **kwargs): an unusual binding runs the wrapper.
        if args.len() < 2 || kwargs.contains("context")? || kwargs.contains("value")? { return Ok(None); }
        let filter = Py::new(py, NativeAsyncIterating { context: args.get_item(0)?.unbind(), value: args.get_item(1)?.unbind(),
            args: args.get_slice(2, args.len()).unbind(), kwargs: kwargs.copy()?.unbind(), mode,
            globals: global(&scope, "ignore_case")?.unbind(), select: None, map: None })?;
        let binding = crate::python_function::binding(py)?;
        return Ok(Some(match mode {
            Some(_) => binding.getattr("_native_async_select")?.call1((*name, filter))?,
            None => binding.getattr("_native_async_map")?.call1((filter, function.getattr("__globals__")?))?,
        }));
    }
    let bound = crate::python_function::bind_fixed(function, args, kwargs)?;
    let step = Py::new(py, NativeAsyncVariant { name, bound: bound.into_iter().map(Bound::unbind).collect(),
        function: function.clone().unbind() })?;
    crate::python_function::binding(py)?.getattr("_native_async_variant")?.call1((*name, step)).map(Some)
}

/// jinja2.async_utils's helpers, captured at first use from the loaded
/// module (initialization must not import it); None until it is loaded.
fn async_utils(py: Python<'_>) -> PyResult<Option<&'static AsyncUtils>> {
    if let Some(utils) = ASYNC_UTILS.get(py) { return Ok(Some(utils)); }
    let Some(module) = crate::find_loaded_module(py, pyo3::intern!(py, "jinja2.async_utils"))? else { return Ok(None); };
    let Ok(module) = module.into_bound(py).cast_into::<pyo3::types::PyModule>() else { return Ok(None); };
    // Only the module whose auto_aiter jinja2.filters imported.
    let Some(filters) = crate::find_loaded_module(py, pyo3::intern!(py, "jinja2.filters"))? else { return Ok(None); };
    let Ok(filters) = filters.into_bound(py).cast_into::<pyo3::types::PyModule>() else { return Ok(None); };
    let imported = filters.dict().get_item("auto_aiter")?.map(|function| function.getattr_opt("__globals__")).transpose()?.flatten();
    if !imported.is_some_and(|globals| globals.is(module.dict())) { return Ok(None); }
    let mut functions = Vec::new();
    for name in ["auto_aiter", "auto_to_list"] {
        let Some(function) = module.dict().get_item(name)? else { return Ok(None); };
        if !function.is_exact_instance_of::<pyo3::types::PyFunction>() { return Ok(None); }
        functions.push((name, StandardFunction::capture_value(&module, name, &function)?));
    }
    let Some(adapter) = module.dict().get_item("_IteratorToAsyncIterator")? else { return Ok(None); };
    if !adapter.is_exact_instance_of::<pyo3::types::PyType>() { return Ok(None); }
    let mut methods = Vec::new();
    for name in ["__init__", "__anext__"] {
        let Ok(method) = adapter.getattr("__dict__")?.get_item(name) else { return Ok(None); };
        if !method.is_exact_instance_of::<pyo3::types::PyFunction>() { return Ok(None); }
        methods.push((name, StandardFunction::capture_value(&module, name, &method)?));
    }
    // A replacement captured here is still checked against the filters'
    // globals, and its globals are never the original module's.
    Ok(Some(ASYNC_UTILS.get_or_init(py, || AsyncUtils { functions, adapter: adapter.unbind(), methods })))
}

fn async_helpers_standard(scope: &FrameGlobals<'_>) -> PyResult<bool> {
    let py = scope_py(scope)?;
    let Some(utils) = async_utils(py)? else { return Ok(false); };
    for (name, original) in &utils.functions {
        if !original.matches(&global(scope, name)?)? { return Ok(false); }
    }
    let auto_aiter = global(scope, "auto_aiter")?;
    let module = FrameGlobals::of_function(&auto_aiter)?;
    let adapter = global(&module, "_IteratorToAsyncIterator")?;
    if !adapter.is(utils.adapter.bind(py)) { return Ok(false); }
    for (name, original) in &utils.methods {
        let Ok(method) = adapter.getattr("__dict__")?.get_item(*name) else { return Ok(false); };
        if !original.matches(&method)? { return Ok(false); }
    }
    let builtins = py.import("builtins")?;
    for name in ["hasattr", "iter", "next"] {
        if !global(&module, name)?.is(&builtins.getattr(name)?) { return Ok(false); }
    }
    Ok(true)
}

/// The awaited step of a ported async body: `(True, result)`, or `(False,
/// coroutine)` with the reference body for an async iterable.
#[pyclass(module = "docxtpl_native._native")]
struct NativeAsyncVariant {
    name: &'static str,
    bound: Vec<Py<PyAny>>,
    function: Py<PyAny>,
}

#[pymethods]
impl NativeAsyncVariant {
    fn __call__(&self, py: Python<'_>) -> PyResult<(bool, Py<PyAny>)> {
        let bound: Vec<Bound<'_, PyAny>> = self.bound.iter().map(|value| value.bind(py).clone()).collect();
        let value = &bound[if matches!(self.name, "do_list" | "do_slice") { 0 } else { 1 }];
        // auto_aiter: an async iterable keeps the reference body.
        if value.getattr_opt(pyo3::intern!(py, "__aiter__"))?.is_some() {
            return Ok((false, self.function.bind(py).call1(PyTuple::new(py, &bound)?)?.unbind()));
        }
        let scope = FrameGlobals::of_function(self.function.bind(py))?;
        let utils = FrameGlobals::of_function(&global(&scope, "auto_aiter")?)?;
        // _IteratorToAsyncIterator(iter(iterable)): next() until exhausted.
        let iterator = global(&utils, "iter")?.call1((value,))?;
        let collect = || -> PyResult<Bound<'_, pyo3::types::PyList>> {
            let items = pyo3::types::PyList::empty(py);
            while let Some(item) = next_of(&iterator)? { items.append(item)?; }
            Ok(items)
        };
        let globals = global(&scope, "ignore_case")?.unbind();
        let result = match self.name {
            "do_first" => match next_of(&iterator)? {
                Some(item) => item.into_bound(py),
                None => bound[0].getattr("undefined")?.call1(("No first item, sequence was empty.",))?,
            },
            "do_list" => collect()?.into_any(),
            "do_sum" => {
                let (environment, attribute) = (&bound[0], &bound[2]);
                let getter = if attribute.is_none() { None }
                    else { Some(attribute_getter(&scope, &globals, environment, attribute, None)?) };
                let mut total = bound[3].clone();
                while let Some(item) = next_of(&iterator)? {
                    let item = match &getter { Some(getter) => getter.bind(py).call1((item,))?, None => item.into_bound(py) };
                    // `rv += func(item)`.
                    let sum = unsafe { pyo3::ffi::PyNumber_InPlaceAdd(total.as_ptr(), item.as_ptr()) };
                    total = unsafe { Bound::from_owned_ptr_or_err(py, sum) }?;
                }
                total
            }
            "do_groupby" => {
                let items = collect()?;
                groupby_filter(&scope, &bound[0], items.as_any(), &bound[2], &bound[3], &bound[4])?
            }
            "do_slice" => filter_generator(py, &scope, FilterIteration::Slice { value: collect()?.into_any().unbind(),
                slices: bound[1].clone().unbind(), fill_with: bound[2].clone().unbind(), prepared: None }, "sync_do_slice")?,
            _ => filter_generator(py, &scope, FilterIteration::Unique { environment: bound[0].clone().unbind(),
                value: collect()?.into_any().unbind(), case_sensitive: bound[2].clone().unbind(),
                attribute: bound[3].clone().unbind(), started: None }, "sync_do_unique")?,
        };
        Ok((true, result.unbind()))
    }
}

/// The async select family and map: the binding's async generators iterate
/// (as `auto_aiter` does) and call `accept` or `apply` per item.
#[pyclass(module = "docxtpl_native._native")]
struct NativeAsyncIterating {
    context: Py<PyAny>,
    value: Py<PyAny>,
    args: Py<PyTuple>,
    kwargs: Py<PyDict>,
    // Some((attribute, reject)) for the select family, None for map.
    mode: Option<(bool, bool)>,
    globals: Py<PyAny>,
    select: Option<SelectTest>,
    map: Option<MapPlan>,
}

#[pymethods]
impl NativeAsyncIterating {
    /// `if value:`, then prepare_select_or_reject / prepare_map, then
    /// auto_aiter's choice: ("empty", None), ("sync", iterator) or ("async",
    /// value.__aiter__()).
    fn start(&mut self, py: Python<'_>) -> PyResult<(&'static str, Py<PyAny>)> {
        let value = self.value.bind(py);
        if !value.is_truthy()? { return Ok(("empty", py.None())); }
        let scope = FrameGlobals::of_function(self.globals.bind(py))?;
        let (context, args) = (self.context.bind(py), self.args.bind(py));
        match self.mode {
            Some((attribute, _)) => self.select = Some(select_test(&scope, &self.globals, context, args, attribute)?),
            None => self.map = Some(map_plan(&scope, &self.globals, context, args, self.kwargs.bind(py))?),
        }
        if value.getattr_opt(pyo3::intern!(py, "__aiter__"))?.is_some() {
            return Ok(("async", value.call_method0(pyo3::intern!(py, "__aiter__"))?.unbind()));
        }
        Ok(("sync", value.try_iter()?.into_any().unbind()))
    }

    fn accept(&self, py: Python<'_>, item: &Bound<'_, PyAny>) -> PyResult<bool> {
        let (Some(test), Some((_, reject))) = (&self.select, self.mode) else {
            return Err(pyo3::exceptions::PyRuntimeError::new_err("filter not started"));
        };
        Ok(select_accepts(test, self.context.bind(py), self.kwargs.bind(py), item)? != reject)
    }

    fn apply(&self, py: Python<'_>, item: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let Some(plan) = &self.map else { return Err(pyo3::exceptions::PyRuntimeError::new_err("filter not started")); };
        map_apply(plan, self.context.bind(py), self.kwargs.bind(py), item).map(Bound::unbind)
    }
}

/// `sync_do_groupby` over `value`.
fn groupby_filter<'py>(scope: &FrameGlobals<'py>, environment: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>,
    attribute: &Bound<'py, PyAny>, default: &Bound<'py, PyAny>, case_sensitive: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = scope_py(scope)?;
    let lower = !case_sensitive.is_truthy()?;
    let default = (!default.is_none()).then(|| default.clone().unbind());
    let attrgetter = |lower: bool| -> PyResult<Py<NativeSortKey>> {
        Py::new(py, NativeSortKey { kind: SortKeyKind::Single, environment: Some(environment.clone().unbind()),
            parts: vec![attribute_parts(scope, attribute)?], lower, default: default.as_ref().map(|value| value.clone_ref(py)),
            globals: global(scope, "ignore_case")?.unbind(), binding: crate::python_function::binding(py)?.unbind() })
    };
    let expression = attrgetter(lower)?;
    let keywords = PyDict::new(py);
    keywords.set_item("key", &expression)?;
    let ordered = global(scope, "sorted")?.call((value,), Some(&keywords))?;
    let group_tuple = global(scope, "_GroupTuple")?;
    let list = global(scope, "list")?;
    let out = pyo3::types::PyList::empty(py);
    for group in global(scope, "groupby")?.call1((ordered, &expression))?.try_iter()? {
        let (key, values) = unpack_pair(&group?)?;
        out.append(group_tuple.call1((key, list.call1((values,))?))?)?;
    }
    if !lower { return Ok(out.into_any()); }
    // The real key from the first value instead of the lowercase key.
    let output = attrgetter(false)?;
    let result = pyo3::types::PyList::empty(py);
    for group in out.iter() {
        let (_, values) = unpack_pair(&group)?;
        result.append(group_tuple.call1((output.bind(py).call1((values.get_item(0)?,))?, values))?)?;
    }
    Ok(result.into_any())
}

/// The state of a filter that Jinja writes as a generator function. Work
/// happens on `__next__`, as in the generator body, never at the call.
enum FilterIteration {
    Items { value: Py<PyAny>, iterator: Option<Py<PyAny>> },
    Batch { source: Py<PyAny>, iterator: Option<Py<PyAny>>, linecount: Py<PyAny>, fill_with: Py<PyAny>, tmp: Py<pyo3::types::PyList> },
    Slice { value: Py<PyAny>, slices: Py<PyAny>, fill_with: Py<PyAny>, prepared: Option<SlicePlan> },
    Unique { environment: Py<PyAny>, value: Py<PyAny>, case_sensitive: Py<PyAny>, attribute: Py<PyAny>,
        started: Option<(Py<PyAny>, Py<NativeSortKey>, Py<pyo3::types::PySet>)> },
    Select { context: Py<PyAny>, value: Py<PyAny>, args: Py<PyTuple>, kwargs: Py<PyDict>, attribute: bool, reject: bool,
        started: Option<SelectPlan> },
    Map { context: Py<PyAny>, value: Py<PyAny>, args: Py<PyTuple>, kwargs: Py<PyDict>, started: Option<(Py<PyAny>, MapPlan)> },
    Done,
}

struct SelectPlan { iterator: Py<PyAny>, test: SelectTest }

/// prepare_select_or_reject's per-item function, before `modfunc`.
struct SelectTest { attribute: Option<Py<NativeSortKey>>, test: Option<(Py<PyAny>, Py<PyTuple>)> }

fn select_test(scope: &FrameGlobals<'_>, globals: &Py<PyAny>, context: &Bound<'_, PyAny>, args: &Bound<'_, PyTuple>,
    attribute: bool) -> PyResult<SelectTest> {
    let offset = usize::from(attribute);
    let getter = if attribute {
        let Ok(name) = args.get_item(0) else {
            return Err(argument_error(scope, "Missing parameter for attribute name")?);
        };
        Some(attribute_getter(scope, globals, &context.getattr("environment")?, &name, None)?)
    } else { None };
    let test = match args.get_item(offset) {
        Ok(name) => Some((name.unbind(), args.get_slice(offset + 1, args.len()).unbind())),
        Err(_) => None,
    };
    Ok(SelectTest { attribute: getter, test })
}

/// Whether the test (or the value's truth) holds for `item`.
fn select_accepts(test: &SelectTest, context: &Bound<'_, PyAny>, kwargs: &Bound<'_, PyDict>, item: &Bound<'_, PyAny>)
    -> PyResult<bool> {
    let py = item.py();
    let mut subject = item.clone();
    if let Some(getter) = &test.attribute { subject = getter.bind(py).call1((subject,))?; }
    match &test.test {
        Some((name, test_args)) => call_filter_or_test(context, name.bind(py), &subject, test_args.bind(py), kwargs, false)?
            .is_truthy(),
        None => subject.is_truthy(),
    }
}

/// prepare_map's function.
fn map_plan(scope: &FrameGlobals<'_>, globals: &Py<PyAny>, context: &Bound<'_, PyAny>, args: &Bound<'_, PyTuple>,
    kwargs: &Bound<'_, PyDict>) -> PyResult<MapPlan> {
    let py = context.py();
    if args.is_empty() && kwargs.contains("attribute")? {
        let attribute = kwargs.call_method1("pop", ("attribute",))?;
        let default = kwargs.call_method1("pop", ("default", py.None()))?;
        if let Some(key) = next_of(&kwargs.try_iter()?.into_any())? {
            let message = format!("Unexpected keyword argument {}", key.bind(py).repr()?);
            return Err(PyErr::from_value(global(scope, "FilterArgumentError")?.call1((message,))?));
        }
        let default = (!default.is_none()).then(|| default.unbind());
        return Ok(MapPlan::Attribute(attribute_getter(scope, globals, &context.getattr("environment")?, &attribute, default)?));
    }
    let Ok(name) = args.get_item(0) else {
        return Err(argument_error(scope, "map requires a filter argument")?);
    };
    Ok(MapPlan::Filter(name.unbind(), args.get_slice(1, args.len()).unbind()))
}

fn map_apply<'py>(plan: &MapPlan, context: &Bound<'py, PyAny>, kwargs: &Bound<'py, PyDict>, item: &Bound<'py, PyAny>)
    -> PyResult<Bound<'py, PyAny>> {
    let py = item.py();
    match plan {
        MapPlan::Attribute(getter) => getter.bind(py).call1((item,)),
        MapPlan::Filter(name, filter_args) => call_filter_or_test(context, name.bind(py), item, filter_args.bind(py), kwargs, true),
    }
}

enum MapPlan { Attribute(Py<NativeSortKey>), Filter(Py<PyAny>, Py<PyTuple>) }

struct SlicePlan { seq: Py<PyAny>, items_per_slice: Py<PyAny>, slices_with_extra: Py<PyAny>, offset: Py<PyAny>, numbers: Py<PyAny> }

#[pyclass(module = "docxtpl_native._native")]
struct NativeFilterIterator {
    state: FilterIteration,
    // The filter function's module globals (builtins, Undefined, abc, ...).
    function: Py<PyAny>,
}

fn filter_generator<'py>(py: Python<'py>, scope: &FrameGlobals<'py>, state: FilterIteration, name: &str)
    -> PyResult<Bound<'py, PyAny>> {
    let function = global(scope, "ignore_case")?;
    let iterator = Py::new(py, NativeFilterIterator { state, function: function.unbind() })?;
    crate::python_function::binding(py)?.getattr("_native_filter_generator")?.call1((iterator, name))
}

#[pymethods]
impl NativeFilterIterator {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }

    fn __next__(&mut self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        let result = self.step(py);
        if !matches!(result, Ok(Some(_))) { self.state = FilterIteration::Done; }
        result
    }
}

impl NativeFilterIterator {
    fn step(&mut self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        let scope = FrameGlobals::of_function(self.function.bind(py))?;
        match &mut self.state {
            FilterIteration::Done => Ok(None),
            FilterIteration::Items { value, iterator } => {
                if iterator.is_none() {
                    let value = value.bind(py);
                    let isinstance = global(&scope, "isinstance")?;
                    if isinstance.call1((value, global(&scope, "Undefined")?))?.is_truthy()? { return Ok(None); }
                    if !isinstance.call1((value, global(&scope, "abc")?.getattr("Mapping")?))?.is_truthy()? {
                        return Err(pyo3::exceptions::PyTypeError::new_err("Can only get item pairs from a mapping."));
                    }
                    *iterator = Some(value.call_method0("items")?.try_iter()?.into_any().unbind());
                }
                next_of(iterator.as_ref().unwrap().bind(py))
            }
            FilterIteration::Batch { source, iterator, linecount, fill_with, tmp } => {
                if iterator.is_none() { *iterator = Some(source.bind(py).try_iter()?.into_any().unbind()); }
                let iterator = iterator.as_ref().unwrap().bind(py);
                loop {
                    match next_of(iterator)? {
                        Some(item) => {
                            let full = tmp.bind(py).len().into_pyobject(py)?.as_any().eq(linecount.bind(py))?;
                            if full {
                                let batch = std::mem::replace(tmp, pyo3::types::PyList::empty(py).unbind());
                                tmp.bind(py).append(item)?;
                                return Ok(Some(batch.into_any()));
                            }
                            tmp.bind(py).append(item)?;
                        }
                        None => {
                            let batch = std::mem::replace(tmp, pyo3::types::PyList::empty(py).unbind());
                            let batch = batch.bind(py);
                            if batch.is_empty() { return Ok(None); }
                            let fill_with = fill_with.bind(py);
                            let size = batch.len().into_pyobject(py)?;
                            if !fill_with.is_none() && size.lt(linecount.bind(py))? {
                                let missing = linecount.bind(py).sub(&size)?;
                                let filler = pyo3::types::PyList::new(py, [fill_with])?.mul(missing)?;
                                let batch = batch.add(filler)?;
                                *tmp = pyo3::types::PyList::empty(py).unbind();
                                return Ok(Some(batch.unbind()));
                            }
                            return Ok(Some(batch.clone().into_any().unbind()));
                        }
                    }
                }
            }
            FilterIteration::Slice { value, slices, fill_with, prepared } => {
                if prepared.is_none() {
                    let seq = global(&scope, "list")?.call1((value.bind(py),))?;
                    let length = global(&scope, "len")?.call1((&seq,))?;
                    let slices = slices.bind(py);
                    *prepared = Some(SlicePlan {
                        items_per_slice: length.floor_div(slices)?.unbind(),
                        slices_with_extra: length.rem(slices)?.unbind(),
                        offset: 0i32.into_pyobject(py)?.into_any().unbind(),
                        numbers: global(&scope, "range")?.call1((slices,))?.try_iter()?.into_any().unbind(),
                        seq: seq.unbind(),
                    });
                }
                let plan = prepared.as_mut().unwrap();
                let Some(number) = next_of(plan.numbers.bind(py))? else { return Ok(None); };
                let number = number.bind(py);
                let per = plan.items_per_slice.bind(py);
                let start = plan.offset.bind(py).add(number.mul(per)?)?;
                if number.lt(plan.slices_with_extra.bind(py))? {
                    plan.offset = plan.offset.bind(py).add(1)?.unbind();
                }
                let end = plan.offset.bind(py).add(number.add(1)?.mul(per)?)?;
                let range = pyo3::types::PySlice::new(py, 0, 0, 1).get_type().call1((start, end))?;
                let tmp = plan.seq.bind(py).get_item(range)?;
                if !fill_with.bind(py).is_none() && number.ge(plan.slices_with_extra.bind(py))? {
                    tmp.call_method1("append", (fill_with.bind(py),))?;
                }
                Ok(Some(tmp.unbind()))
            }
            FilterIteration::Select { context, value, args, kwargs, attribute, reject, started } => {
                if started.is_none() {
                    if !value.bind(py).is_truthy()? { return Ok(None); }
                    let test = select_test(&scope, &self.function, context.bind(py), args.bind(py), *attribute)?;
                    let iterator = value.bind(py).try_iter()?.into_any().unbind();
                    *started = Some(SelectPlan { iterator, test });
                }
                let plan = started.as_ref().unwrap();
                loop {
                    let Some(item) = next_of(plan.iterator.bind(py))? else { return Ok(None); };
                    if select_accepts(&plan.test, context.bind(py), kwargs.bind(py), item.bind(py))? != *reject {
                        return Ok(Some(item));
                    }
                }
            }
            FilterIteration::Map { context, value, args, kwargs, started } => {
                if started.is_none() {
                    if !value.bind(py).is_truthy()? { return Ok(None); }
                    let plan = map_plan(&scope, &self.function, context.bind(py), args.bind(py), kwargs.bind(py))?;
                    *started = Some((value.bind(py).try_iter()?.into_any().unbind(), plan));
                }
                let (iterator, plan) = started.as_ref().unwrap();
                let Some(item) = next_of(iterator.bind(py))? else { return Ok(None); };
                map_apply(plan, context.bind(py), kwargs.bind(py), item.bind(py)).map(|value| Some(value.unbind()))
            }
            FilterIteration::Unique { environment, value, case_sensitive, attribute, started } => {
                if started.is_none() {
                    let lower = !case_sensitive.bind(py).is_truthy()?;
                    let getter = Py::new(py, NativeSortKey { kind: SortKeyKind::Single, environment: Some(environment.clone_ref(py)),
                        parts: vec![attribute_parts(&scope, attribute.bind(py))?], lower, default: None,
                        globals: self.function.clone_ref(py), binding: crate::python_function::binding(py)?.unbind() })?;
                    let seen = pyo3::types::PySet::empty(py)?.unbind();
                    let iterator = value.bind(py).try_iter()?.into_any().unbind();
                    *started = Some((iterator, getter, seen));
                }
                let (iterator, getter, seen) = started.as_ref().unwrap();
                loop {
                    let Some(item) = next_of(iterator.bind(py))? else { return Ok(None); };
                    let key = getter.bind(py).call1((item.bind(py),))?;
                    if !seen.bind(py).contains(&key)? {
                        seen.bind(py).add(key)?;
                        return Ok(Some(item));
                    }
                }
            }
        }
    }
}

/// `make_attrgetter(environment, attribute, default=default)`.
fn attribute_getter(scope: &FrameGlobals<'_>, globals: &Py<PyAny>, environment: &Bound<'_, PyAny>,
    attribute: &Bound<'_, PyAny>, default: Option<Py<PyAny>>) -> PyResult<Py<NativeSortKey>> {
    let py = environment.py();
    Py::new(py, NativeSortKey { kind: SortKeyKind::Single, environment: Some(environment.clone().unbind()),
        parts: vec![attribute_parts(scope, attribute)?], lower: false, default, globals: globals.clone_ref(py),
        binding: crate::python_function::binding(py)?.unbind() })
}

/// `raise FilterArgumentError(message) from None`.
fn argument_error(scope: &FrameGlobals<'_>, message: &str) -> PyResult<PyErr> {
    let error = global(scope, "FilterArgumentError")?.call1((message,))?;
    error.setattr("__suppress_context__", true)?;
    Ok(PyErr::from_value(error))
}

/// `context.environment.call_filter(name, value, args, kwargs, context=context)`
/// or `.call_test(name, value, args, kwargs, context)`: the filter or test
/// is looked up and called as Jinja does, standard filters natively.
fn call_filter_or_test<'py>(context: &Bound<'py, PyAny>, name: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>,
    args: &Bound<'py, PyTuple>, kwargs: &Bound<'py, PyDict>, filter: bool) -> PyResult<Bound<'py, PyAny>> {
    let py = context.py();
    let environment = context.getattr("environment")?;
    let method = if filter { "call_filter" } else { "call_test" };
    if !environment_standard(&environment, method)? {
        return if filter {
            let keywords = PyDict::new(py);
            keywords.set_item("context", context)?;
            environment.call_method(method, (name, value, args, kwargs), Some(&keywords))
        } else { environment.call_method1(method, (name, value, args, kwargs, context)) };
    }
    let common = environment.get_type().getattr("_filter_test_common")?;
    let scope = FrameGlobals::of_function(&common)?;
    let (mapping, kind) = if filter { ("filters", "filter") } else { ("tests", "test") };
    let function = environment.getattr(mapping)?.call_method1("get", (name,))?;
    if function.is_none() {
        let mut message = format!("No {kind} named {}.", name.repr()?);
        if name.is_instance(&global(&scope, "Undefined")?)?
            && let Err(error) = name.call_method0("_fail_with_undefined_error") {
            if !error.is_instance_of::<pyo3::exceptions::PyException>(py) { return Err(error); }
            message = format!("{message} ({}; did you forget to quote the callable name?)", error.value(py).str()?);
        }
        return Err(PyErr::from_value(global(&scope, "TemplateRuntimeError")?.call1((message,))?));
    }
    let mut call_args = vec![value.clone()];
    call_args.extend(args.iter());
    if let Some(pass_arg) = function.getattr_opt(pyo3::intern!(py, "jinja_pass_arg"))? {
        let kinds = global(&scope, "_PassArg")?;
        if pass_arg.is(&kinds.getattr("context")?) {
            call_args.insert(0, context.clone());
        } else if pass_arg.is(&kinds.getattr("eval_context")?) {
            call_args.insert(0, context.getattr("eval_ctx")?);
        } else if pass_arg.is(&kinds.getattr("environment")?) {
            call_args.insert(0, environment.clone());
        }
    }
    let call_args = PyTuple::new(py, call_args)?;
    let keywords = kwargs.copy()?;
    match try_call(&function, &call_args, &keywords)? {
        Some(result) => Ok(result),
        None => function.call(call_args, Some(&keywords)),
    }
}

/// Whether `environment` dispatches `method` with Jinja's own Environment code.
fn environment_standard(environment: &Bound<'_, PyAny>, method: &str) -> PyResult<bool> {
    let py = environment.py();
    let Some(functions) = ENVIRONMENT_FUNCTIONS.get(py) else { return Ok(false); };
    if functions.len() != ENVIRONMENT_METHODS.len() { return Ok(false); }
    let instance = environment.getattr_opt(pyo3::intern!(py, "__dict__"))?;
    let class = environment.get_type();
    for (name, original) in functions {
        if *name != method && *name != "_filter_test_common" { continue; }
        if let Some(instance) = &instance && instance.contains(*name)? { return Ok(false); }
        if !original.matches(&class.getattr(*name)?)? { return Ok(false); }
    }
    Ok(true)
}

/// `f"{value}"`.
fn format_value(value: &Bound<'_, PyAny>) -> PyResult<String> {
    let py = value.py();
    let spec = pyo3::types::PyString::new(py, "");
    let formatted = unsafe { pyo3::ffi::PyObject_Format(value.as_ptr(), spec.as_ptr()) };
    unsafe { Bound::from_owned_ptr_or_err(py, formatted) }?.extract()
}

/// Whether the class member `name` of the standard Markup is its original.
fn markup_method_standard(py: Python<'_>, name: &str) -> PyResult<bool> {
    let Some(original) = markup_standard(py, name)? else { return Ok(false); };
    let Some((_, member)) = markup_member(py, name)? else { return Ok(false); };
    original.matches(&member)
}

/// Python's `str.isspace` for one character: Rust's White_Space plus the
/// four information separators.
fn python_space(character: char) -> bool {
    character.is_whitespace() || ('\x1c'..='\x1f').contains(&character)
}

/// `Markup.striptags` before its final unescape.
fn strip_tags(value: &str) -> String {
    let mut parts = String::new();
    let mut position = 0;
    while let Some(offset) = value[position..].find('<') {
        let start = position + offset;
        let end = if value[start..].starts_with("<!--") {
            match value[start + 4..].find("-->") { Some(end) => start + 4 + end + 3, None => break }
        } else {
            match value[start..].find('>') { Some(end) => start + end + 1, None => break }
        };
        parts.push_str(&value[position..start]);
        position = end;
    }
    let value = if position > 0 { parts.push_str(&value[position..]); parts.as_str() } else { value };
    value.split(python_space).filter(|part| !part.is_empty()).collect::<Vec<_>>().join(" ")
}

/// striptags, xmlattr and urlize, or None to run the Python body.
fn markup_filter<'py>(name: &str, scope: &FrameGlobals<'py>, args: &[Bound<'py, PyAny>])
    -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = scope_py(scope)?;
    if !helpers_standard(scope)? { return Ok(None); }
    let markup = global(scope, "Markup")?;
    match name {
        "do_striptags" => {
            if standard_markup_constructor(&markup)?.is_none() || !markup_method_standard(py, "striptags")?
                || !markup_method_standard(py, "unescape")? { return Ok(None); }
            let mut value = args[0].clone();
            if value.getattr_opt(pyo3::intern!(py, "__html__"))?.is_some() { value = value.call_method0("__html__")?; }
            let text = global(scope, "str")?.call1((value,))?;
            // Markup(text) of an exact str holds text itself.
            let text = if text.is_exact_instance_of::<pyo3::types::PyString>() { text }
                else { return markup.call1((text,))?.call_method0("striptags").map(Some); };
            let stripped = strip_tags(&text.extract::<String>()?);
            py.import("html")?.getattr("unescape")?.call1((stripped,)).map(Some)
        }
        "do_xmlattr" => {
            let (eval_context, mapping, autospace) = (&args[0], &args[1], &args[2]);
            let (isinstance, undefined) = (global(scope, "isinstance")?, global(scope, "Undefined")?);
            let (escape, pattern) = (global(scope, "escape")?, global(scope, "_attr_key_re")?);
            let mut items = Vec::new();
            for item in mapping.call_method0("items")?.try_iter()? {
                let (key, value) = unpack_pair(&item?)?;
                if value.is_none() || isinstance.call1((&value, &undefined))?.is_truthy()? { continue; }
                if !pattern.call_method1("search", (&key,))?.is_none() {
                    let message = format!("Invalid character in attribute name: {}", key.repr()?);
                    return Err(PyErr::from_value(global(scope, "ValueError")?.call1((message,))?));
                }
                items.push(format!("{}=\"{}\"", format_value(&escape_value(&escape, &key)?)?,
                    format_value(&escape_value(&escape, &value)?)?));
            }
            let mut text = items.join(" ");
            if autospace.is_truthy()? && !text.is_empty() { text.insert(0, ' '); }
            if eval_context.getattr("autoescape")?.is_truthy()? { return markup.call1((text,)).map(Some); }
            Ok(Some(pyo3::types::PyString::new(py, &text).into_any()))
        }
        "do_urlize" => {
            let (eval_context, value, trim_url_limit, nofollow) = (&args[0], &args[1], &args[2], &args[3]);
            let (mut target, rel, mut extra_schemes) = (args[4].clone(), &args[5], args[6].clone());
            let policies = eval_context.getattr("environment")?.getattr("policies")?;
            let empty = pyo3::types::PyString::new(py, "").into_any();
            let rel_parts = global(scope, "set")?.call1(((if rel.is_truthy()? { rel } else { &empty }).call_method0("split")?,))?;
            if nofollow.is_truthy()? { rel_parts.call_method1("add", ("nofollow",))?; }
            let policy = policies.get_item("urlize.rel")?;
            rel_parts.call_method1("update", ((if policy.is_truthy()? { &policy } else { &empty }).call_method0("split")?,))?;
            let joined = pyo3::types::PyString::new(py, " ").call_method1("join", (global(scope, "sorted")?.call1((rel_parts,))?,))?;
            let rel = if joined.is_truthy()? { joined } else { py.None().into_bound(py) };
            if target.is_none() { target = policies.get_item("urlize.target")?; }
            if extra_schemes.is_none() {
                let policy = policies.get_item("urlize.extra_schemes")?;
                extra_schemes = if policy.is_truthy()? { policy } else { PyTuple::empty(py).into_any() };
            }
            let pattern = global(scope, "_uri_scheme_re")?;
            for scheme in extra_schemes.try_iter()? {
                let scheme = scheme?;
                if pattern.call_method1("fullmatch", (&scheme,))?.is_none() {
                    let message = format!("{} is not a valid URI scheme prefix.", scheme.repr()?);
                    return Err(PyErr::from_value(global(scope, "FilterArgumentError")?.call1((message,))?));
                }
            }
            let utils = FrameGlobals::of_function(&global(scope, "urlize")?)?;
            let text = urlize(&utils, value, trim_url_limit, &rel, &target, &extra_schemes)?;
            if eval_context.getattr("autoescape")?.is_truthy()? { return markup.call1((text,)).map(Some); }
            Ok(Some(text))
        }
        _ => unreachable!(),
    }
}

/// The byte offset of character `index` of `text`.
fn char_offset(text: &str, index: usize) -> usize {
    text.char_indices().nth(index).map_or(text.len(), |(offset, _)| offset)
}

/// `jinja2.utils.urlize`, with the `re` and `markupsafe` it imports.
fn urlize<'py>(utils: &FrameGlobals<'py>, text: &Bound<'py, PyAny>, trim_url_limit: &Bound<'py, PyAny>,
    rel: &Bound<'py, PyAny>, target: &Bound<'py, PyAny>, extra_schemes: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = utils.py();
    let (re, markupsafe, string) = (global(utils, "re")?, global(utils, "markupsafe")?, global(utils, "str")?);
    let (http, email) = (global(utils, "_http_re")?, global(utils, "_email_re")?);
    let trim_url = |value: &str| -> PyResult<String> {
        if trim_url_limit.is_none() { return Ok(value.to_owned()); }
        let value = pyo3::types::PyString::new(py, value);
        if !value.len()?.into_pyobject(py)?.as_any().gt(trim_url_limit)? { return Ok(value.to_string()); }
        let slice = pyo3::types::PySlice::new(py, 0, 0, 1).get_type().call1((py.None(), trim_url_limit))?;
        Ok(format!("{}...", format_value(&value.get_item(slice)?)?))
    };
    let escaped = string.call1((escape_value(&markupsafe.getattr("escape")?, text)?,))?;
    let words = re.call_method1("split", (r"(\s+)", escaped))?;
    let attribute = |name: &str, value: &Bound<'py, PyAny>| -> PyResult<String> {
        if !value.is_truthy()? { return Ok(String::new()); }
        Ok(format!(" {name}=\"{}\"", format_value(&escape_value(&markupsafe.getattr("escape")?, value)?)?))
    };
    let rel_attr = attribute("rel", rel)?;
    let target_attr = attribute("target", target)?;
    let mut output = String::new();
    for word in words.try_iter()? {
        let word: String = word?.extract()?;
        let (mut head, mut middle, mut tail) = (String::new(), word, String::new());
        let found = re.call_method1("match", (r"^([(<]|&lt;)+", &middle))?;
        if found.is_truthy()? {
            head = found.call_method0("group")?.extract()?;
            middle = middle[head.len()..].to_owned();
        }
        if [")", ">", ".", ",", "\n", "&gt;"].iter().any(|end| middle.ends_with(end)) {
            let found = re.call_method1("search", (r"([)>.,\n]|&gt;)+$", &middle))?;
            if found.is_truthy()? {
                tail = found.call_method0("group")?.extract()?;
                middle.truncate(char_offset(&middle, found.call_method0("start")?.extract()?));
            }
        }
        for (start, end) in [("(", ")"), ("<", ">"), ("&lt;", "&gt;")] {
            let start_count = middle.matches(start).count();
            if start_count <= middle.matches(end).count() { continue; }
            for _ in 0..start_count.min(tail.matches(end).count()) {
                let end_index = tail.find(end).unwrap() + end.len();
                middle.push_str(&tail[..end_index]);
                tail = tail[end_index..].to_owned();
            }
        }
        if http.call_method1("match", (&middle,))?.is_truthy()? {
            let scheme = if middle.starts_with("https://") || middle.starts_with("http://") { "" } else { "https://" };
            middle = format!("<a href=\"{scheme}{middle}\"{rel_attr}{target_attr}>{}</a>", trim_url(&middle)?);
        } else if middle.starts_with("mailto:") && email.call_method1("match", (&middle[7..],))?.is_truthy()? {
            middle = format!("<a href=\"{middle}\">{}</a>", &middle[7..]);
        } else if middle.contains('@') && !middle.starts_with("www.") && !middle.starts_with('@') && !middle.contains(':')
            && email.call_method1("match", (&middle,))?.is_truthy()? {
            middle = format!("<a href=\"mailto:{middle}\">{middle}</a>");
        } else if !extra_schemes.is_none() {
            for scheme in extra_schemes.try_iter()? {
                let scheme = scheme?;
                let current = pyo3::types::PyString::new(py, &middle);
                if current.as_any().ne(&scheme)? && current.call_method1("startswith", (&scheme,))?.is_truthy()? {
                    middle = format!("<a href=\"{middle}\"{rel_attr}{target_attr}>{middle}</a>");
                }
            }
        }
        output.push_str(&head);
        output.push_str(&middle);
        output.push_str(&tail);
    }
    Ok(pyo3::types::PyString::new(py, &output).into_any())
}

fn next_of(iterator: &Bound<'_, PyAny>) -> PyResult<Option<Py<PyAny>>> {
    let py = iterator.py();
    let value = unsafe { pyo3::ffi::PyIter_Next(iterator.as_ptr()) };
    if value.is_null() {
        return match PyErr::take(py) { Some(error) => Err(error), None => Ok(None) };
    }
    Ok(Some(unsafe { Bound::from_owned_ptr(py, value) }.unbind()))
}

fn scope_py<'py>(scope: &FrameGlobals<'py>) -> PyResult<Python<'py>> {
    Ok(global(scope, "sorted")?.py())
}

/// Jinja's `_prepare_attribute_parts`: dotted names with digit parts as ints.
fn attribute_parts<'py>(scope: &FrameGlobals<'py>, attribute: &Bound<'py, PyAny>) -> PyResult<Vec<Py<PyAny>>> {
    if attribute.is_none() { return Ok(Vec::new()); }
    if global(scope, "isinstance")?.call1((attribute, global(scope, "str")?))?.is_truthy()? {
        let int = global(scope, "int")?;
        let mut parts = Vec::new();
        for part in attribute.call_method1("split", (".",))?.try_iter()? {
            let part = part?;
            parts.push(if part.call_method0("isdigit")?.is_truthy()? { int.call1((&part,))?.unbind() } else { part.unbind() });
        }
        return Ok(parts);
    }
    Ok(vec![attribute.clone().unbind()])
}

/// The key functions of `sort` (`make_multi_attrgetter`), `min`/`max`
/// (`make_attrgetter`) and `dictsort` (`sort_func`), with `ignore_case`.
#[pyclass(module = "docxtpl_native._native", frozen)]
struct NativeSortKey {
    kind: SortKeyKind,
    environment: Option<Py<PyAny>>,
    parts: Vec<Vec<Py<PyAny>>>,
    lower: bool,
    // make_attrgetter's default: replaces an Undefined result of a part.
    default: Option<Py<PyAny>>,
    // ignore_case: its module globals resolve isinstance and str.
    globals: Py<PyAny>,
    binding: Py<PyModule>,
}

enum SortKeyKind { Single, Multi, DictItem(usize) }

impl NativeSortKey {
    fn ignore_case<'py>(&self, py: Python<'py>, value: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let scope = FrameGlobals::of_function(self.globals.bind(py))?;
        if global(&scope, "isinstance")?.call1((&value, global(&scope, "str")?))?.is_truthy()? {
            value.call_method0("lower")
        } else { Ok(value) }
    }

    fn follow<'py>(&self, py: Python<'py>, mut item: Bound<'py, PyAny>, parts: &[Py<PyAny>]) -> PyResult<Bound<'py, PyAny>> {
        let environment = self.environment.as_ref().expect("attribute keys have the environment");
        for part in parts {
            // As make_attrgetter: environment.getitem is read for each part.
            let getitem = environment.bind(py).getattr("getitem")?;
            item = crate::python_lookup::dispatch_lookup(&getitem, &item, part.bind(py), false, self.binding.bind(py))?;
            if let Some(default) = &self.default {
                let scope = FrameGlobals::of_function(self.globals.bind(py))?;
                if global(&scope, "isinstance")?.call1((&item, global(&scope, "Undefined")?))?.is_truthy()? {
                    item = default.bind(py).clone();
                }
            }
        }
        if self.lower { self.ignore_case(py, item) } else { Ok(item) }
    }
}

#[pymethods]
impl NativeSortKey {
    fn __call__<'py>(&self, py: Python<'py>, item: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        match self.kind {
            SortKeyKind::DictItem(position) => {
                let value = item.get_item(position)?;
                if self.lower { self.ignore_case(py, value) } else { Ok(value) }
            }
            SortKeyKind::Single => self.follow(py, item, &self.parts[0]),
            SortKeyKind::Multi => {
                let values = pyo3::types::PyList::empty(py);
                for parts in &self.parts { values.append(self.follow(py, item.clone(), parts)?)?; }
                Ok(values.into_any())
            }
        }
    }
}

/// `do_sort`, `do_dictsort`, `do_min` and `do_max` with the original helpers:
/// the same argument evaluation order, `sorted`/`min`/`max`/`iter`/`next`
/// and `chain` from the module, and the environment's `getitem`.
fn ordering_filter<'py>(name: &str, scope: &FrameGlobals<'py>, args: &[Bound<'py, PyAny>]) -> PyResult<Bound<'py, PyAny>> {
    let py = scope_py(scope)?;
    let binding = crate::python_function::binding(py)?.unbind();
    let ignore = global(scope, "ignore_case")?.unbind();
    let key = |kind: SortKeyKind, environment: Option<Py<PyAny>>, parts: Vec<Vec<Py<PyAny>>>, lower: bool| {
        Py::new(py, NativeSortKey { kind, environment, parts, lower, default: None, globals: ignore.clone_ref(py), binding: binding.clone_ref(py) })
    };
    let keywords = PyDict::new(py);
    match name {
        "do_dictsort" => {
            let (value, case_sensitive, by, reverse) = (&args[0], &args[1], &args[2], &args[3]);
            let position = if by.eq("key")? { 0 } else if by.eq("value")? { 1 } else {
                return Err(PyErr::from_value(global(scope, "FilterArgumentError")?
                    .call1(("You can only sort by either \"key\" or \"value\"",))?));
            };
            let lower = !case_sensitive.is_truthy()?;
            keywords.set_item("key", key(SortKeyKind::DictItem(position), None, Vec::new(), lower)?)?;
            keywords.set_item("reverse", reverse)?;
            global(scope, "sorted")?.call((value.call_method0("items")?,), Some(&keywords))
        }
        "do_sort" => {
            let (environment, value, reverse, case_sensitive, attribute) = (&args[0], &args[1], &args[2], &args[3], &args[4]);
            let lower = !case_sensitive.is_truthy()?;
            let split: Vec<Bound<'py, PyAny>> = if global(scope, "isinstance")?.call1((attribute, global(scope, "str")?))?.is_truthy()? {
                attribute.call_method1("split", (",",))?.try_iter()?.collect::<PyResult<_>>()?
            } else { vec![attribute.clone()] };
            let parts = split.iter().map(|attribute| attribute_parts(scope, attribute)).collect::<PyResult<Vec<_>>>()?;
            keywords.set_item("key", key(SortKeyKind::Multi, Some(environment.clone().unbind()), parts, lower)?)?;
            keywords.set_item("reverse", reverse)?;
            global(scope, "sorted")?.call((value,), Some(&keywords))
        }
        _ => {
            let (environment, value, case_sensitive, attribute) = (&args[0], &args[1], &args[2], &args[3]);
            let iterator = global(scope, "iter")?.call1((value,))?;
            let first = match global(scope, "next")?.call1((&iterator,)) {
                Ok(first) => first,
                Err(error) if error.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => {
                    return environment.getattr("undefined")?.call1(("No aggregated item, sequence was empty.",));
                }
                Err(error) => return Err(error),
            };
            let lower = !case_sensitive.is_truthy()?;
            let parts = attribute_parts(scope, attribute)?;
            keywords.set_item("key", key(SortKeyKind::Single, Some(environment.clone().unbind()), vec![parts], lower)?)?;
            let chained = global(scope, "chain")?.call1((pyo3::types::PyList::new(py, [first])?, iterator))?;
            global(scope, if name == "do_min" { "min" } else { "max" })?.call((chained,), Some(&keywords))
        }
    }
}

fn inplace_add<'py>(left: &Bound<'py, PyAny>, right: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    unsafe { Bound::from_owned_ptr_or_err(left.py(), pyo3::ffi::PyNumber_InPlaceAdd(left.as_ptr(), right.as_ptr())) }
}

fn indent_filter<'py>(scope: &FrameGlobals<'py>, args: &[Bound<'py, PyAny>]) -> PyResult<Bound<'py, PyAny>> {
    use pyo3::types::PyString;
    let py = scope.py();
    let mut indentation = if global(scope, "isinstance")?
        .call1((&args[1], global(scope, "str")?))?.is_truthy()? {
        args[1].clone()
    } else {
        crate::python_operators::python_operator(py, "mul", PyString::new(py, " ").as_any(), &args[1])?
    };
    let mut newline = PyString::new(py, "\n").into_any();
    if global(scope, "isinstance")?.call1((&args[0], global(scope, "Markup")?))?.is_truthy()? {
        indentation = global(scope, "Markup")?.call1((indentation,))?;
        newline = global(scope, "Markup")?.call1((newline,))?;
    }
    let value = inplace_add(&args[0], &newline)?;
    // In the reference `lines` is a function local and remains alive while
    // `first` is evaluated, even if a custom splitlines result has a destructor.
    let mut retained_lines = None;
    let result = if args[3].is_truthy()? {
        let separator = crate::python_operators::python_operator(py, "add", &newline, &indentation)?;
        let join = separator.getattr("join")?;
        drop(separator);
        let lines = value.call_method0("splitlines")?;
        let result = join.call1((lines,))?;
        drop(join);
        result
    } else {
        retained_lines = Some(std::sync::Arc::new(value.call_method0("splitlines")?.unbind()));
        let lines = retained_lines.as_ref().unwrap().bind(py);
        let mut result = lines.call_method1("pop", (0,))?;
        if lines.is_truthy()? {
            let join = newline.getattr("join")?;
            let iterator = lines.try_iter()?.into_any().unbind();
            let adapter = Py::new(py, IndentIterator {
                iterator: Some(iterator), indentation: Some(indentation.clone().unbind()), current: None,
                lines: retained_lines.clone(), failed: false,
            })?;
            let binding = crate::python_function::binding(py)?;
            let generator = binding.getattr("_indent_generator")?.call1((adapter.clone_ref(py),))?;
            let arguments = [generator.as_ptr()];
            let joined = unsafe {
                Bound::from_owned_ptr_or_err(py, pyo3::ffi::PyObject_Vectorcall(
                    join.as_ptr(), arguments.as_ptr(), arguments.len(), std::ptr::null_mut(),
                ))
            };
            // A generator closed before its iterator was exhausted can have
            // been terminated by throw(). Its traceback retains the generator
            // frame and the adapter, so keep lines there until that frame dies.
            // A saved, still-open generator must not extend lines' lifetime.
            let generator_closed = if joined.is_ok() { generator.getattr("gi_frame")?.is_none() } else { false };
            drop(generator);
            let unused_lines = {
                let mut adapter = adapter.borrow_mut(py);
                if joined.is_ok() && !adapter.failed && (!generator_closed || adapter.iterator.is_none()) {
                    adapter.lines.take()
                } else { None }
            };
            drop(unused_lines);
            drop(adapter);
            let joined = joined?;
            drop(join);
            let suffix = crate::python_operators::python_operator(py, "add", &newline, &joined)?;
            drop(joined);
            result = inplace_add(&result, &suffix)?;
        }
        result
    };
    let output = if args[2].is_truthy()? {
        crate::python_operators::python_operator(py, "add", &indentation, &result)
    } else { Ok(result) };
    drop(retained_lines);
    output
}

fn soft_str<'py>(
    scope: &FrameGlobals<'py>,
    value: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let function = global(scope, "soft_str")?;
    let standard = match SOFT_STR.get(value.py()).and_then(Option::as_ref) {
        Some(original) => original.matches(&function)?,
        None => false,
    };
    if !standard {
        return function.call1((value,));
    }
    let globals = FrameGlobals::of_function(&function)?;
    soft_str_body(&globals, value)
}

fn soft_str_body<'py>(globals: &FrameGlobals<'py>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    if global(&globals, "isinstance")?
        .call1((value, global(&globals, "str")?))?
        .is_truthy()?
    {
        Ok(value.clone())
    } else {
        let string = global(&globals, "str")?;
        if string.is(&value.py().get_type::<pyo3::types::PyString>()) {
            if let Some(text) = crate::python_operators::native_default_image_string(value, false)? {
                return Ok(text);
            }
        }
        string.call1((value,))
    }
}

fn format_filter<'py>(
    function: &Bound<'py, PyAny>,
    positional: &Bound<'py, PyTuple>,
    keywords: &Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = function.py();
    let code = function.getattr("__code__")?;
    let names = code.getattr("co_varnames")?.cast_into::<PyTuple>()?;
    let parameter = names.get_item(0)?;
    let mut value = if positional.is_empty() { None } else { Some(positional.get_item(0)?) };
    let extras = PyDict::new(py);
    for pair in crate::python_keywords::snapshot(keywords)?.iter() {
        let key = pair.get_item(0)?;
        let item = pair.get_item(1)?;
        if crate::python_keywords::parameter_index(&key, std::slice::from_ref(&parameter))?.is_some() {
            if value.is_some() {
                return Err(crate::python_keywords::argument_error(
                    function, &key, "() got multiple values for argument '",
                )?);
            }
            value = Some(item);
        } else {
            extras.set_item(key, item)?;
        }
    }
    if value.is_none() {
        let defaults = function.getattr("__defaults__")?;
        if !defaults.is_none() {
            let defaults = defaults.cast::<PyTuple>()?;
            if !defaults.is_empty() { value = Some(defaults.get_item(defaults.len() - 1)?); }
        }
    }
    let value = match value {
        Some(value) => value,
        None => return Err(crate::python_keywords::function_error(
            function, "() missing 1 required positional argument: 'value'",
        )?),
    };
    let arguments = PyTuple::new(py, positional.iter().skip(1))?;
    let scope = FrameGlobals::of_function(function)?;
    if !arguments.is_empty() && !extras.is_empty() {
        return Err(PyErr::from_value(global(&scope, "FilterArgumentError")?.call1((
            "can't handle positional and keyword arguments at the same time",
        ))?));
    }
    let format = soft_str(&scope, &value)?;
    let operand = if extras.is_empty() { arguments.into_any() } else { extras.into_any() };
    crate::python_operators::python_operator(py, "rem", &format, &operand)
}

fn default_format<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    // FORMAT_VALUE without a conversion calls the object's format protocol.
    unsafe { Bound::from_owned_ptr_or_err(value.py(), pyo3::ffi::PyObject_Format(value.as_ptr(), std::ptr::null_mut())) }
}

fn assertion_error(message: &Bound<'_, PyAny>) -> PyErr {
    // Raising through the C API preserves the currently handled exception.
    unsafe { pyo3::ffi::PyErr_SetObject(pyo3::ffi::PyExc_AssertionError, message.as_ptr()); }
    PyErr::fetch(message.py())
}

fn truncate_filter<'py>(scope: &FrameGlobals<'py>, args: &[Bound<'py, PyAny>]) -> PyResult<Bound<'py, PyAny>> {
    let py = scope.py();
    let env = &args[0];
    let value = &args[1];
    let length = &args[2];
    let killwords = &args[3];
    let end = &args[4];
    let leeway = if args[5].is_none() { env.getattr("policies")?.get_item("truncate.leeway")? }
        else { args[5].clone() };
    let len = |v: &Bound<'py, PyAny>| global(scope, "len")?.call1((v,));
    let op = |name: &str, left: &Bound<'py, PyAny>, right: &Bound<'py, PyAny>|
        crate::python_operators::python_operator(py, name, left, right);

    // LOAD_ASSERTION_ERROR and the tests do not exist in optimized bytecode.
    #[allow(deprecated)]
    let assertions_enabled = unsafe { pyo3::ffi::Py_OptimizeFlag == 0 };
    if assertions_enabled {
        let end_length = len(end)?;
        if !op(">=", length, &end_length)?.is_truthy()? {
            let repeated_length = len(end)?;
            let message = crate::python_operators::join_strings(py, vec![
                pyo3::types::PyString::new(py, "expected length >= ").into_any(),
                default_format(&repeated_length)?,
                pyo3::types::PyString::new(py, ", got ").into_any(),
                default_format(length)?,
            ], None)?;
            return Err(assertion_error(&message));
        }
        let zero = 0i32.into_pyobject(py)?.into_any();
        if !op(">=", &leeway, &zero)?.is_truthy()? {
            let message = crate::python_operators::join_strings(py, vec![
                pyo3::types::PyString::new(py, "expected leeway >= 0, got ").into_any(),
                default_format(&leeway)?,
            ], None)?;
            return Err(assertion_error(&message));
        }
    }
    let value_length = len(value)?;
    let allowed = op("add", length, &leeway)?;
    if op("<=", &value_length, &allowed)?.is_truthy()? {
        return Ok(value.clone());
    }
    let kill = killwords.is_truthy()?;
    let end_length = len(end)?;
    let stop = op("sub", length, &end_length)?;
    let slice = py.get_type::<pyo3::types::PySlice>().call1((py.None(), stop, py.None()))?;
    let mut result = value.get_item(slice)?;
    if !kill {
        result = result.call_method1("rsplit", (" ", 1))?.get_item(0)?;
    }
    op("add", &result, end)
}

fn filesize_filter<'py>(scope: &FrameGlobals<'py>, args: &[Bound<'py, PyAny>]) -> PyResult<Bound<'py, PyAny>> {
    use pyo3::types::{PyList, PyString};
    let py = scope.py();
    let bytes = global(scope, "float")?.call1((&args[0],))?;
    let binary = &args[1];
    let base = (if binary.is_truthy()? { 1024i32 } else { 1000i32 }).into_pyobject(py)?.into_any();
    let mut prefixes = Vec::with_capacity(8);
    for (decimal, binary_prefix) in [("kB", "KiB"), ("MB", "MiB"), ("GB", "GiB"),
        ("TB", "TiB"), ("PB", "PiB"), ("EB", "EiB"), ("ZB", "ZiB"), ("YB", "YiB")] {
        prefixes.push(if binary.is_truthy()? { binary_prefix } else { decimal });
    }
    let op = |name: &str, left: &Bound<'py, PyAny>, right: &Bound<'py, PyAny>|
        crate::python_operators::python_operator(py, name, left, right);
    let one = 1i32.into_pyobject(py)?.into_any();
    if op("==", &bytes, &one)?.is_truthy()? {
        return Ok(PyString::new(py, "1 Byte").into_any());
    }
    if op("<", &bytes, &base)?.is_truthy()? {
        let value = global(scope, "int")?.call1((&bytes,))?;
        return crate::python_operators::join_strings(py, vec![
            default_format(&value)?, PyString::new(py, " Bytes").into_any(),
        ], None);
    }
    let prefixes = PyList::new(py, prefixes)?;
    let pairs = global(scope, "enumerate")?.call1((prefixes,))?;
    let two = 2i32.into_pyobject(py)?.into_any();
    let mut last = None;
    for pair in pairs.try_iter()? {
        let (index, prefix) = crate::unpack_pair(&pair?)?;
        let exponent = op("add", &index, &two)?;
        let unit = op("pow", &base, &exponent)?;
        let done = op("<", &bytes, &unit)?.is_truthy()?;
        last = Some((unit, prefix));
        if done { break; }
    }
    let Some((unit, prefix)) = last else {
        return Err(pyo3::exceptions::PyUnboundLocalError::new_err(
            "cannot access local variable 'unit' where it is not associated with a value"));
    };
    let product = op("mul", &base, &bytes)?;
    let result = op("div", &product, &unit)?;
    let spec = PyString::new(py, ".1f");
    let formatted = unsafe {
        Bound::from_owned_ptr_or_err(py, pyo3::ffi::PyObject_Format(result.as_ptr(), spec.as_ptr()))?
    };
    crate::python_operators::join_strings(py, vec![
        formatted, PyString::new(py, " ").into_any(), default_format(&prefix)?,
    ], None)
}

pub(crate) fn try_call<'py>(
    function: &Bound<'py, PyAny>,
    args: &Bound<'py, PyTuple>,
    kwargs: &Bound<'py, PyDict>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = function.py();
    if args.len() == 1 && kwargs.is_empty() && docxtpl_core::has_owned_callback_image_scope() {
        if let Some(original) = SOFT_STR.get(py).and_then(Option::as_ref) {
            if original.matches(function)? {
                let value = args.get_item(0)?;
                if crate::python_bridge::concrete_default_image(&value)?
                    && crate::python_bridge::default_image_protocol(&value, false)? {
                    let scope = FrameGlobals::of_function(function)?;
                    return soft_str_body(&scope, &value).map(Some);
                }
            }
        }
    }
    // The `string` and `escape` filters are MarkupSafe's own functions.
    if args.len() == 1 && kwargs.is_empty() {
        let value = args.get_item(0)?;
        if SOFT_STR.get(py).and_then(Option::as_ref).map(|original| original.matches(function)).transpose()? == Some(true) {
            return soft_str_body(&FrameGlobals::of_function(function)?, &value).map(Some);
        }
        if ESCAPE.get(py).and_then(Option::as_ref).map(|original| original.matches(function)).transpose()? == Some(true) {
            return escape_value(function, &value).map(Some);
        }
    }
    if let Some(wrappers) = VARIANT_WRAPPERS.get(py) {
        for (name, wrapper, code) in wrappers {
            if function.is(wrapper.bind(py)) && function.getattr("__code__")?.is(code.bind(py)) {
                return async_variant_filter(name, function, args, kwargs);
            }
        }
    }
    let Some(functions) = FUNCTIONS.get(py) else {
        return Ok(None);
    };
    for (name, original) in functions {
        if !original.matches(function)? {
            continue;
        }
        if *name == "do_format" {
            return format_filter(function, args, kwargs).map(Some);
        }

        let args = crate::python_function::bind_fixed(function, args, kwargs)?;
        let scope = FrameGlobals::of_function(function)?;
        let value = &args[0];
        return Ok(Some(match *name {
            "do_forceescape" => forceescape(&scope, value)?,
            "do_replace" => replace_filter(&scope, &args)?,
            "do_indent" => indent_filter(&scope, &args)?,
            "do_sort" | "do_dictsort" | "do_min" | "do_max" => {
                if helpers_standard(&scope)? { ordering_filter(name, &scope, &args)? }
                else { function.call1(PyTuple::new(py, &args)?)? }
            }
            "do_batch" | "do_items" => {
                let state = if *name == "do_batch" {
                    FilterIteration::Batch { source: args[0].clone().unbind(), iterator: None, linecount: args[1].clone().unbind(),
                        fill_with: args[2].clone().unbind(), tmp: pyo3::types::PyList::empty(py).unbind() }
                } else { FilterIteration::Items { value: args[0].clone().unbind(), iterator: None } };
                filter_generator(py, &scope, state, name)?
            }
            "do_urlencode" | "do_wordwrap" | "do_pprint" | "do_random" | "do_attr" => {
                if helpers_standard(&scope)? { text_filter(name, &scope, &args)? }
                else { function.call1(PyTuple::new(py, &args)?)? }
            }
            "do_mark_safe" | "do_mark_unsafe" => {
                let constructor = global(&scope, if *name == "do_mark_safe" { "Markup" } else { "str" })?;
                if *name == "do_mark_safe" {
                    match markup_default_image(&constructor, value)? {
                        Some(result) => result,
                        None => constructor.call1((value,))?,
                    }
                } else { constructor.call1((value,))? }
            }
            "do_truncate" => truncate_filter(&scope, &args)?,
            "do_striptags" | "do_xmlattr" | "do_urlize" => match markup_filter(name, &scope, &args)? {
                Some(result) => result,
                None => function.call1(PyTuple::new(py, &args)?)?,
            },
            "do_filesizeformat" => filesize_filter(&scope, &args)?,
            "do_title" => {
                let join = pyo3::types::PyString::new(py, "").getattr("join")?;
                let split = global(&scope, "_word_beginning_split_re")?.getattr("split")?;
                let parts = split.call1((soft_str(&scope, value)?,))?;
                let result = pyo3::types::PyList::empty(py);
                for part in parts.try_iter()? {
                    let part = part?;
                    if part.is_truthy()? {
                        let first = part.get_item(0)?.call_method0("upper")?;
                        let slice = py.get_type::<pyo3::types::PySlice>().call1((1, py.None(), py.None()))?;
                        let rest = part.get_item(slice)?.call_method0("lower")?;
                        result.append(crate::python_operators::python_operator(py, "add", &first, &rest)?)?;
                    }
                }
                join.call1((result,))?
            }
            "do_upper" | "do_lower" | "do_capitalize" => {
                soft_str(&scope, value)?.call_method0(&name[3..])?
            }
            "do_center" | "do_trim" => soft_str(&scope, value)?.call_method1(
                if *name == "do_center" {
                    "center"
                } else {
                    "strip"
                },
                (&args[1],),
            )?,
            "do_float" => match global(&scope, "float")?.call1((value,)) {
                Ok(value) => value,
                Err(error) => {
                    handle_exception(&scope, error, &["TypeError", "ValueError"], || {
                        Ok(args[1].clone())
                    })?
                }
            },
            "do_int" => {
                let attempt = (|| {
                    if global(&scope, "isinstance")?
                        .call1((value, global(&scope, "str")?))?
                        .is_truthy()?
                    {
                        global(&scope, "int")?.call1((value, &args[2]))
                    } else {
                        global(&scope, "int")?.call1((value,))
                    }
                })();
                match attempt {
                    Ok(value) => value,
                    Err(error) => {
                        handle_exception(&scope, error, &["TypeError", "ValueError"], || {
                            let attempt = (|| {
                                let convert = global(&scope, "int")?;
                                convert.call1((global(&scope, "float")?.call1((value,))?,))
                            })();
                            match attempt {
                                Ok(value) => Ok(value),
                                Err(error) => handle_exception(
                                    &scope,
                                    error,
                                    &["TypeError", "ValueError", "OverflowError"],
                                    || Ok(args[1].clone()),
                                ),
                            }
                        })?
                    }
                }
            }
            "do_default" => {
                if global(&scope, "isinstance")?
                    .call1((value, global(&scope, "Undefined")?))?
                    .is_truthy()?
                    || (args[2].is_truthy()? && !value.is_truthy()?)
                {
                    args[1].clone()
                } else {
                    value.clone()
                }
            }
            "do_reverse" => {
                if global(&scope, "isinstance")?
                    .call1((value, global(&scope, "str")?))?
                    .is_truthy()?
                {
                    let slice = py.get_type::<pyo3::types::PySlice>().call1((py.None(), py.None(), -1))?;
                    value.get_item(slice)?
                } else {
                    match global(&scope, "reversed")?.call1((value,)) {
                        Ok(result) => result,
                        Err(error) => handle_exception(&scope, error, &["TypeError"], || {
                            let result: PyResult<_> = (|| {
                                let list = global(&scope, "list")?.call1((value,))?;
                                list.call_method0("reverse")?;
                                Ok(list)
                            })();
                            match result {
                                Ok(list) => Ok(list),
                                Err(error) => handle_exception(&scope, error.clone_ref(py), &["TypeError"], || {
                                    let failure = PyErr::from_value(global(&scope, "FilterArgumentError")?
                                        .call1(("argument must be iterable",))?);
                                    failure.set_cause(py, Some(error));
                                    Err(failure)
                                }),
                            }
                        })?,
                    }
                }
            }
            "do_last" => {
                let result = (|| {
                    let next = global(&scope, "next")?;
                    let iter = global(&scope, "iter")?;
                    let reverse = global(&scope, "reversed")?;
                    next.call1((iter.call1((reverse.call1((&args[1],))?,))?,))
                })();
                match result {
                    Ok(result) => result,
                    Err(error) => handle_exception(&scope, error, &["StopIteration"], || {
                        value.call_method1("undefined", ("No last item, sequence was empty.",))
                    })?,
                }
            }
            "do_wordcount" => {
                let length = global(&scope, "len")?;
                let pattern = global(&scope, "_word_re")?;
                let find = pattern.getattr("findall")?;
                let text = soft_str(&scope, value)?;
                if WORD_RE
                    .get(py)
                    .is_some_and(|original| pattern.is(original.bind(py)))
                {
                    let version = match (py.version_info().major, py.version_info().minor) {
                        (3, 11) => docxtpl_core::UnicodeWordVersion::Python311,
                        (3, 12) => docxtpl_core::UnicodeWordVersion::Python312,
                        _ => docxtpl_core::UnicodeWordVersion::Unqualified,
                    };
                    let text = text.cast::<pyo3::types::PyString>()?;
                    let size = unsafe { pyo3::ffi::PyUnicode_GetLength(text.as_ptr()) };
                    let mut in_word = false;
                    // Retain matched strings for a user replacement of len.
                    // Reading Unicode through the C API includes lone surrogates.
                    let matches = pyo3::types::PyList::empty(py);
                    let mut start = 0;
                    for i in 0..=size {
                        let word = i < size
                            && char::from_u32(unsafe {
                                pyo3::ffi::PyUnicode_ReadChar(text.as_ptr(), i)
                            })
                            .is_some_and(|c| docxtpl_core::word_character(c, version));
                        if word && !in_word {
                            start = i;
                        }
                        if !word && in_word {
                            let matched = unsafe {
                                Bound::from_owned_ptr_or_err(
                                    py,
                                    pyo3::ffi::PyUnicode_Substring(text.as_ptr(), start, i),
                                )?
                            };
                            matches.append(matched)?;
                        }
                        in_word = word;
                    }
                    length.call1((matches,))?
                } else {
                    length.call1((find.call1((text,))?,))?
                }
            }
            "do_round" => {
                let method = &args[2];
                let methods = pyo3::types::PySet::new(py, ["common", "ceil", "floor"])?;
                if !methods.contains(method)? {
                    return Err(PyErr::from_value(
                        global(&scope, "FilterArgumentError")?
                            .call1(("method must be common, ceil or floor",))?,
                    ));
                }
                if method.eq("common")? {
                    global(&scope, "round")?.call1((value, &args[1]))?
                } else {
                    let math = global(&scope, "math")?;
                    let function = global(&scope, "getattr")?.call1((math, method))?;
                    let cast = global(&scope, "t")?.getattr("cast")?;
                    let class = global(&scope, "float")?;
                    let ten = 10i64.into_pyobject(py)?.into_any();
                    let power =
                        crate::python_operators::python_operator(py, "pow", &ten, &args[1])?;
                    let scaled =
                        crate::python_operators::python_operator(py, "mul", value, &power)?;
                    let rounded = function.call1((scaled,))?;
                    let power =
                        crate::python_operators::python_operator(py, "pow", &ten, &args[1])?;
                    let result =
                        crate::python_operators::python_operator(py, "div", &rounded, &power)?;
                    let standard = match CAST.get(py).and_then(Option::as_ref) {
                        Some(original) => original.matches(&cast)?,
                        None => false,
                    };
                    if standard {
                        result
                    } else {
                        cast.call1((class, result))?
                    }
                }
            }
            _ => unreachable!(),
        }));
    }
    Ok(None)
}
