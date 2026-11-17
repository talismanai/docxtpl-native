//! First integration. Unsupported Python semantics are rejected by the frontend;
//! native generation never falls back to the reference implementation.
use docxtplrs::template::{EnvCache, HostFragmentConverter, HostFragmentRecovery, HostFragmentRenderer, HostUnicodeMaterializer, HostTemplate, HostTemplateRenderer,
    OwnedMaterializationFragment, TplCore};
use docxtpl_core::{context_value, context_value_with_provenance, prepare_shared, process_model_shared, process_shared_with_numbering_protocols, save_compatible, save_rendered_compatible, save_recovered_body_compatible, xml_check};
use docxtpl_core::opc;
#[cfg(test)]
use docxtpl_core::complete_core_properties;
#[cfg(test)]
use minijinja::Value;
use pyo3::exceptions::{PyRuntimeError, PyStopIteration, PyTypeError, PyUnicodeEncodeError, PyValueError};
use pyo3::prelude::*;
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::types::{PyBytes, PyDict, PyIterator, PyString, PyTuple};
use serde_json::Value as Json;
use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

mod fiber_local;
mod native_package;
mod python_bridge;
mod python_ast;
mod richtext_binding;
mod python_operators;
mod python_format;
mod python_call;
mod python_macro;
mod python_keywords;
mod python_function;
mod python_context;
mod python_context_helpers;
mod python_lookup;
mod python_sandbox;
mod native_tests;
mod native_filters;
mod native_join;
mod numbering_integer;
mod constant_eval;
mod xml_binding;
mod python_session;
mod python_traceback;
mod python_async;
mod python_deferred;
mod python_loop;
mod python_module;
mod python_blocks;
mod python_errors;
mod inline_image;
mod live_xml;
mod live_properties;
mod live_footnotes;
mod live_headers;
mod live_body;

// The reference module keeps `docx` as a global. Cache the already-imported
// module at extension initialization without invoking a user import hook later.
static DOCX_MODULE: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();
static OPC_XML_MODULE: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();
static LXML_ETREE_MODULE: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();
static LXML_PROVIDER_PATH: pyo3::sync::PyOnceLock<std::path::PathBuf> = pyo3::sync::PyOnceLock::new();
static XML_PART_CLASS: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();

fn find_loaded_module(py: Python<'_>, name: &Bound<'_, PyString>) -> PyResult<Option<Py<PyAny>>> {
    // SAFETY: CPython owns this dictionary for the interpreter lifetime;
    // the GIL is held and Bound acquires its own reference.
    let modules = unsafe { Bound::from_borrowed_ptr(py, pyo3::ffi::PyImport_GetModuleDict()) };
    Ok(modules.cast::<PyDict>()?.get_item(name)?.map(Bound::unbind))
}

fn find_loaded_docx(py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
    find_loaded_module(py, pyo3::intern!(py, "docx"))
}

fn docx_module(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    DOCX_MODULE.get_or_try_init(py, || find_loaded_docx(py)?
        .ok_or_else(|| PyRuntimeError::new_err("docx is required for the exposed document model")))
}

fn opc_xml_module(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    OPC_XML_MODULE.get_or_try_init(py, || {
        find_loaded_module(py, pyo3::intern!(py, "docx.opc.oxml"))?
            .ok_or_else(|| PyRuntimeError::new_err("docx OPC parser is required for the exposed document model"))
    })
}

fn xml_part_class(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    XML_PART_CLASS.get_or_try_init(py, || {
        let module = find_loaded_module(py, pyo3::intern!(py, "docx.opc.part"))?
            .ok_or_else(|| PyRuntimeError::new_err("docx OPC model is required for the exposed document"))?;
        Ok(module.bind(py).getattr(pyo3::intern!(py, "XmlPart"))?.unbind())
    })
}

/// Keep exact Python bytes alive without copying them before the detached call.
/// Other Python inputs retain the previous Vec<u8> extraction semantics.
enum SourceBuffer {
    Python(pyo3::pybacked::PyBackedBytes),
    Owned(Vec<u8>),
}

fn capture_source(source: &Bound<'_, PyAny>) -> PyResult<SourceBuffer> {
    if source.is_exact_instance_of::<PyBytes>() {
        Ok(SourceBuffer::Python(source.extract()?))
    } else {
        Ok(SourceBuffer::Owned(source.extract()?))
    }
}

impl SourceBuffer {
    fn into_shared(self) -> Arc<[u8]> {
        match self {
            SourceBuffer::Python(source) => Arc::from(source.as_ref()),
            SourceBuffer::Owned(source) => source.into(),
        }
    }
}

fn error(message: impl ToString) -> PyErr {
    let message = message.to_string();
    if let Some(diagnostic) = message.strip_prefix("DOCXTPL_HEADER_XML_ERROR:") {
        if let Ok(diagnostic) = serde_json::from_str::<Json>(diagnostic) {
            if let (Some(detail), Some(code), Some(line), Some(column)) = (
                diagnostic["message"].as_str(), diagnostic["code"].as_i64(),
                diagnostic["line"].as_i64(), diagnostic["column"].as_i64(),
            ) {
                return Python::attach(|py| -> PyErr {
                    // The reference reaches this failure by handing the rendered
                    // part to python-docx's parser, and an lxml exception carries
                    // the error log of the parse that raised it. Parse the same
                    // bytes here so the log is the reference's, and keep the
                    // diagnostic as the fallback when that parser disagrees.
                    if let Some(source) = diagnostic["source"].as_str() {
                        let raised = (|| -> PyResult<Option<PyErr>> {
                            let parse = py.import("docx.oxml")?.getattr("parse_xml")?;
                            let class = py.import("lxml.etree")?.getattr("XMLSyntaxError")?;
                            match parse.call1((PyBytes::new(py, source.as_bytes()),)) {
                                Err(error) if error.is_instance(py, &class) => Ok(Some(error)),
                                _ => Ok(None),
                            }
                        })();
                        if let Ok(Some(error)) = raised { return error; }
                    }
                    let result = (|| -> PyResult<PyErr> {
                        let class = py.import("lxml.etree")?.getattr("XMLSyntaxError")?;
                        let formatted = format!("{detail}, line {line}, column {column}");
                        let instance = class.call1((formatted, code, line, column, "<string>"))?;
                        Ok(PyErr::from_value(instance))
                    })();
                    match result { Ok(error) => error, Err(error) => error }
                });
            }
        }
    }
    if let Some(name) = message.strip_prefix("invalid ZIP part ")
        .and_then(|value| value.strip_suffix(": Invalid checksum"))
    {
        // Translate the native ZIP failure into the reference's Python API
        // exception while leaving integrity checking inside Rust.
        return Python::attach(|py| -> PyErr {
            let result = (|| -> PyResult<PyErr> {
                let quoted = PyString::new(py, name).repr()?.extract::<String>()?;
                let class = py.import("zipfile")?.getattr("BadZipFile")?;
                let instance = class.call1((format!("Bad CRC-32 for file {quoted}"),))?;
                Ok(PyErr::from_value(instance))
            })();
            match result {
                Ok(error) => error,
                Err(error) => error,
            }
        });
    }
    PyRuntimeError::new_err(message)
}

/// Own libxml2 implementation of the reference's six property assignments.
/// Render even empty/literal fields: taking the context snapshot itself can
/// have Python effects, and property setters validate the rendered string.
fn render_python_properties(core: &mut TplCore, make_ctx: &docxtplrs::template::CtxFn<'_>) -> Result<(), String> {
    const NAME: &str = "docProps/core.xml";
    const DC: &str = "http://purl.org/dc/elements/1.1/";
    let package = core.package.as_mut().ok_or("package not loaded")?;
    if !package.contains(NAME) { docxtplrs::package::ensure_core_part(package); }
    let source = package.get_arc(NAME).ok_or("core properties missing")?;
    let options = libxml::parser::ParserOptions { recover: false, no_net: true, no_blanks: true, ..Default::default() };
    let document = libxml::parser::Parser::default().parse_string_with_options(&*source, options)
        .map_err(|_| "invalid core properties XML".to_owned())?;
    let mut root = document.get_root_element().ok_or("missing core properties root")?;
    let namespaces = root.get_namespaces(&document);
    let namespace = match namespaces.iter().find(|ns| ns.get_href() == DC) {
        Some(namespace) => namespace.clone(),
        None => {
            let prefix = (0..).map(|number| format!("docxtpldc{number}"))
                .find(|prefix| !namespaces.iter().any(|ns| ns.get_prefix() == *prefix)).unwrap();
            libxml::tree::Namespace::new(&prefix, DC, &mut root).map_err(|e| e.to_string())?
        }
    };
    for name in ["creator", "description", "identifier", "language", "subject", "title"] {
        let existing = root.get_child_elements().into_iter().find(|node|
            node.get_name() == name && node.get_namespace().is_some_and(|ns| ns.get_href() == DC));
        let initial = existing.as_ref().map(|node| node.get_content()).unwrap_or_default();
        // Compile before snapshotting the caller's mapping. The independent
        // environment handle releases the core borrow without adding synthetic
        // property names to the loader's namespace.
        let environment = core.env_caches[0].as_ref().ok_or("property environment missing")?.env.clone();
        let (context, rendered) = if let Some(renderer) = core.host_template_renderer.clone() {
            let template = environment.compile_owned_template("<string>".to_owned(), initial.clone())
                .map_err(|e| e.to_string())?;
            let context = make_ctx(core, NAME)?;
            let rendered = (renderer.0)(environment, HostTemplate::Owned(template)).map_err(|e| e.to_string())?;
            (context, rendered)
        } else {
            let template = environment.template_from_str(&initial).map_err(|e| e.to_string())?;
            let context = make_ctx(core, NAME)?;
            let rendered = template.render(&context).map_err(|e| e.to_string())?;
            (context, rendered)
        };
        let rendered = core.materialize_deferred(NAME, rendered)?;
        if rendered.chars().count() > 255 {
            return Err(python_bridge::property_value_error(&context,
                format!("exceeded 255 char limit for property, got:\n\n'{}'", rendered)));
        }
        if rendered.chars().any(|ch| !matches!(ch, '\t' | '\n' | '\r' | '\u{20}'..='\u{d7ff}' | '\u{e000}'..='\u{fffd}' | '\u{10000}'..='\u{10ffff}')) {
            return Err(python_bridge::property_value_error(&context,
                "All strings must be XML compatible: Unicode or ASCII, no NULL bytes or control characters".to_owned()));
        }
        let mut element = match existing {
            Some(element) => element,
            None => root.new_child(Some(namespace.clone()), name).map_err(|e| e.to_string())?,
        };
        for mut child in element.get_child_nodes() { child.unlink(); }
        let mut text = libxml::tree::Node::new_text(&rendered, &document).map_err(|_| "cannot create property text")?;
        element.add_child(&mut text)?;
    }
    let xml = format!("<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n{}", docxtpl_core::opc::node_to_utf8_string(&document, &root));
    core.package.as_mut().unwrap().set(NAME, xml.into_bytes());
    core.invalidate_part(NAME);
    Ok(())
}

fn render_file_async(bridge: Arc<python_bridge::Bridge>, environment: minijinja::Environment<'static>,
    template: HostTemplate) -> Result<String, minijinja::Error> {
    let task_bridge = bridge.clone();
    let result = Python::attach(|py| python_async::run(py, bridge.clone(), move || {
        let context = task_bridge.snapshot()?;
        let rendered = match template {
            HostTemplate::Named(name) => environment.get_template(&name).and_then(|template| {
                match environment.fragment_joiner() {
                    Some(joiner) => template.render_with_capture_fragments(&context, joiner),
                    None => template.render(&context),
                }
            }),
            HostTemplate::Owned(template) => {
                let template = environment.template_from_owned(&template);
                match environment.fragment_joiner() {
                    Some(joiner) => template.render_with_capture_fragments(&context, joiner),
                    None => template.render(&context),
                }
            },
        };
        // After the render, possibly on another thread (see fiber_local).
        if let Some(original) = task_bridge.take_error() {
            python_async::release(rendered);
            return Err(fiber_local::attach(move |py| python_errors::escaped_iteration(
                py, original, python_errors::Boundary::AsyncGenerator)));
        }
        fiber_local::attach(move |py| {
            let text = rendered.map_err(error)?;
            use pyo3::IntoPyObjectExt;
            text.into_py_any(py)
        })
    })).map_err(|error| bridge.remember(error))?;
    Python::attach(|py| result.bind(py).extract::<String>()).map_err(|error| bridge.remember(error))
}

/// Drive the ordinary asyncio trampoline while retaining root fragment origins.
fn render_file_async_fragments(bridge: Arc<python_bridge::Bridge>, environment: minijinja::Environment<'static>,
    template: HostTemplate) -> Result<Vec<minijinja::Value>, minijinja::Error> {
    let slot = Arc::new(std::sync::Mutex::new(None));
    let rendered_slot = slot.clone();
    let task_bridge = bridge.clone();
    let result = Python::attach(|py| python_async::run(py, bridge.clone(), move || {
        let context = task_bridge.snapshot()?;
        let joiner = environment.fragment_joiner().ok_or_else(|| error("fragment joiner is not configured"))?;
        let rendered = match template {
            HostTemplate::Named(name) => environment.get_template(&name)
                .and_then(|template| template.render_unjoined_fragments(&context, joiner)),
            HostTemplate::Owned(template) => environment.template_from_owned(&template)
                .render_unjoined_fragments(&context, joiner),
        };
        // After the render, possibly on another thread (see fiber_local).
        if let Some(original) = task_bridge.take_error() {
            python_async::release(rendered);
            return Err(fiber_local::attach(move |py| python_errors::escaped_iteration(
                py, original, python_errors::Boundary::AsyncGenerator)));
        }
        let values = rendered.map_err(error)?;
        fiber_local::attach(|py| -> PyResult<Py<PyAny>> {
            let mut text = String::new();
            let mut wide = false;
            for value in &values {
                if let Some(part) = value.as_str() { text.push_str(part); }
                else {
                    let part = task_bridge.python(py, value)?;
                    match part.bind(py).extract::<String>() {
                        Ok(part) => text.push_str(&part),
                        Err(failure) if failure.is_instance_of::<PyUnicodeEncodeError>(py) => { wide = true; break; }
                        Err(failure) => return Err(failure),
                    }
                }
            }
            let output = if wide {
                let parts = values.iter().map(|value| {
                    if let Some(text) = value.as_str() { Ok(PyString::new(py, text).into_any()) }
                    else { task_bridge.python(py, value).map(|value| value.into_bound(py)) }
                }).collect::<PyResult<Vec<_>>>()?;
                python_operators::join_strings(py, parts, None)?.unbind()
            } else { PyString::new(py, &text).into_any().unbind() };
            // Without deferred pieces the returned string is entirely text,
            // so retaining it would only introduce an observable reference.
            if values.iter().any(|value| value.string_provenance().is_some()) {
                *rendered_slot.lock().map_err(|_| PyRuntimeError::new_err("fragment render result is unavailable"))? =
                    Some((values, output.clone_ref(py)));
            }
            Ok(output)
        })
    })).map_err(|error| bridge.remember(error))?;
    let saved = slot.lock().map_err(|_| minijinja::Error::new(
        minijinja::ErrorKind::InvalidOperation, "fragment render result is unavailable"))?.take();
    if let Some((values, original)) = saved {
        if Python::attach(|py| result.bind(py).is(original.bind(py))) { return Ok(values); }
        return Err(Python::attach(|py| bridge.remember(python_bridge::unsupported(py,
            "modified async render output with deferred values needs provenance recovery"))));
    }
    // Output from an unstarted coroutine, or from a render without deferred
    // pieces, is untrusted text and can never designate a deferred index.
    Python::attach(|py| match result.bind(py).extract::<String>() {
        Ok(text) => Ok(vec![minijinja::Value::from(text)]),
        Err(error) if error.is_instance_of::<PyUnicodeEncodeError>(py) => Ok(vec![bridge.opaque_value(result.bind(py))]),
        Err(error) => Err(error),
    })
        .map_err(|error| bridge.remember(error))
}

#[pyfunction]
#[pyo3(signature = (source, payload, autoescape=false, render=true, model_serialized=false, source_rendered=false))]
fn process_docx(py: Python<'_>, source: &Bound<'_, PyAny>, payload: String, autoescape: bool, render: bool,
    model_serialized: bool, source_rendered: bool) -> PyResult<Py<PyBytes>> {
    if model_serialized && render { return Err(PyValueError::new_err("a serialized model cannot be rendered again")); }
    if source_rendered && render { return Err(PyValueError::new_err("rendered output cannot be rendered again")); }
    let source = capture_source(source)?;
    // All !Send Rust state is created and destroyed inside this closure.
    // The integer hook reattaches only when a numbering id is converted.
    let bytes = py.detach(move || {
        let slot = Arc::new(std::sync::Mutex::new(None::<PyErr>));
        let integer_slot = slot.clone();
        let integer = docxtplrs::template::NumberingInteger(Arc::new(move |value| {
            Python::attach(|py| numbering_integer::parse_abstract_id(py, value)).map_err(|error| {
                if let Ok(mut slot) = integer_slot.lock() { *slot = Some(error); }
                "numbering integer protocol failed".to_owned()
            })
        }));
        let result = if model_serialized { process_model_shared(source.into_shared(), &payload) }
            else { process_shared_with_numbering_protocols(source.into_shared(), &payload, autoescape, render, source_rendered, integer) };
        let mut bytes = result.map_err(|message| {
            slot.lock().ok().and_then(|mut slot| slot.take()).unwrap_or_else(|| error(message))
        })?;
        zip_metadata::normalize_permissions(&mut bytes).map_err(error)?;
        Ok::<_, PyErr>(bytes)
    })?;
    Ok(PyBytes::new(py, &bytes).unbind())
}

/// Recover the document part's body as docxtpl's `fix_tables` does with
/// lxml's recover mode, when it is not well-formed. Whether it changed.
fn recover_rendered_document(core: &mut docxtplrs::template::TplCore) -> Result<bool, String> {
    use docxtplrs::template::DOCUMENT_PART;
    core.flush_parts()?;
    let Some(package) = core.package.as_mut() else { return Ok(false); };
    let Some(xml) = package.get_string(DOCUMENT_PART) else { return Ok(false); };
    if docxtplrs::recover::is_well_formed(&xml) { return Ok(false); }
    let encoding = package.encoding_of(DOCUMENT_PART);
    let recovered = docxtplrs::recover::recover_document(&xml);
    package.set(DOCUMENT_PART, docxtplrs::package::encode_part(&recovered, &encoding));
    core.invalidate_part(DOCUMENT_PART);
    Ok(true)
}

#[pyfunction]
#[pyo3(signature = (source, context, configuration, payload, autoescape=false, owner=None))]
fn process_python_docx(py: Python<'_>, source: &Bound<'_, PyAny>, context: Py<PyAny>, configuration: Py<PyAny>,
    payload: String, autoescape: bool, owner: Option<Py<PyAny>>) -> PyResult<(Py<PyBytes>, Py<PyDict>)> {
    let source = capture_source(source)?;
    let recovery_provider = LXML_PROVIDER_PATH.get(py).cloned().or_else(|| loaded_lxml_provider_path(py));
    let (bytes, callbacks, formats, fast_formats) = py.detach(move || -> PyResult<_> {
        let payload: Json = serde_json::from_str(&payload).map_err(error)?;
        let mut core = prepare_shared(source.into_shared(), &payload["replacements"]).map_err(error)?;
        core.per_part_context = true;
        core.host_capture_joining = true;
        core.python_image_contract = true;
        if let Some(owner) = &owner {
            let identity = owner.as_ptr() as usize;
            core.host_part_scope = Some(docxtplrs::template::HostPartScope(Arc::new(move |core, part, render|
                docxtpl_core::with_callback_image_scope(core, part, render, identity))));
        } else {
            core.host_part_scope = Some(docxtplrs::template::HostPartScope(Arc::new(
                docxtpl_core::with_registered_image_scope)));
        }
        core.python_source_contract = true;
        core.header_footer_normalizer = Some(opc::normalize_xml_part);
        core.property_renderer = Some(render_python_properties);
        let has_subdoc = payload["specials"].as_array().is_some_and(|specials| {
            specials.iter().any(|special| special["value"]["kind"].as_str() == Some("subdoc"))
        });
        if has_subdoc {
            core.prepare_python_subdoc_identifiers().map_err(error)?;
        }
        let has_specials = payload["specials"].as_array().is_some_and(|specials| !specials.is_empty());
        let bridge = python_bridge::Bridge::new(configuration, context, true, has_specials)?;
        core.python_subdoc_numbering_contract = true;
        let integer_bridge = bridge.clone();
        core.numbering_integer = Some(docxtplrs::template::NumberingInteger(Arc::new(move |value| {
            Python::attach(|py| numbering_integer::parse_abstract_id(py, value)).map_err(|error| {
                integer_bridge.remember(error);
                "numbering integer protocol failed".to_owned()
            })
        })));
        core.host_fragment_recovery = Some(opc::recover_rendered_xml);
        let invalid_recovered = Arc::new(AtomicBool::new(false));
        let recovery_invalid = invalid_recovered.clone();
        let recovery_bridge = bridge.clone();
        core.host_fragment_recovery_adapter = Some(HostFragmentRecovery(Arc::new(move |source| {
            // Repair the text first so lxml's recover mode keeps entities and
            // stray `&`/`<` (intentional divergence from docxtpl).
            let repaired = docxtplrs::recover::recover(source);
            let recovered = Python::attach(|py| recover_lxml_unicode_with_path(py, &PyString::new(py, &repaired), recovery_provider.as_deref()))
                .map_err(|failure| recovery_bridge.remember(failure).to_string())?;
            if xml_check(recovered.as_bytes()).is_err() {
                recovery_invalid.store(true, Ordering::Relaxed);
            }
            Ok(recovered)
        })));
        let unicode_bridge = bridge.clone();
        core.host_unicode_materializer = Some(HostUnicodeMaterializer(Arc::new(move |core, part, fragments| {
            let codepoints = docxtpl_core::unicode_transport::materialize_fragments(core, part, fragments)?;
            Python::attach(|py| -> PyResult<String> {
                let length = isize::try_from(codepoints.len()).map_err(error)?;
                let source = unsafe { Bound::from_owned_ptr_or_err(py, pyo3::ffi::PyUnicode_FromKindAndData(
                    4, codepoints.as_ptr().cast(), length))? }.cast_into::<PyString>()?;
                if part != docxtplrs::template::DOCUMENT_PART {
                    // Headers and footers encode the rendered string before parsing.
                    source.call_method1("encode", ("utf-8",))?;
                    return Err(python_bridge::unsupported(py, "Unicode recovery for this document part is unavailable"));
                }
                recover_lxml_unicode(py, &source)
            }).map_err(|failure| unicode_bridge.remember(failure).to_string())
        })));
        if bridge.asynchronous {
            let async_bridge = bridge.clone();
            core.host_template_renderer = Some(HostTemplateRenderer(Arc::new(move |environment, template| {
                render_file_async(async_bridge.clone(), environment, template)
            })));
            let fragment_bridge = bridge.clone();
            core.host_fragment_renderer = Some(HostFragmentRenderer(Arc::new(move |environment, template| {
                render_file_async_fragments(fragment_bridge.clone(), environment, template)
            })));
        }
        let converter_bridge = bridge.clone();
        core.host_fragment_converter = Some(HostFragmentConverter(Arc::new(move |values| {
            values.into_iter().map(|value| {
                if let Some(index) = value.string_provenance() {
                    return Ok(OwnedMaterializationFragment::Deferred(index as usize));
                }
                if let Some(text) = value.as_str() {
                    return Ok(OwnedMaterializationFragment::Text(text.to_owned()));
                }
                Python::attach(|py| -> PyResult<OwnedMaterializationFragment> {
                    let text = converter_bridge.python(py, &value)?;
                    match text.bind(py).extract::<String>() {
                        Ok(text) => Ok(OwnedMaterializationFragment::Text(text)),
                        Err(failure) if failure.is_instance_of::<PyUnicodeEncodeError>(py) => {
                            let length = unsafe { pyo3::ffi::PyUnicode_GetLength(text.bind(py).as_ptr()) };
                            if length < 0 { return Err(PyErr::fetch(py)); }
                            let allocation = unsafe { pyo3::ffi::PyUnicode_AsUCS4Copy(text.bind(py).as_ptr()) };
                            if allocation.is_null() { return Err(PyErr::fetch(py)); }
                            struct UnicodeAllocation(*mut pyo3::ffi::Py_UCS4);
                            impl Drop for UnicodeAllocation {
                                fn drop(&mut self) { unsafe { pyo3::ffi::PyMem_Free(self.0.cast()) } }
                            }
                            let allocation = UnicodeAllocation(allocation);
                            let codepoints = unsafe { std::slice::from_raw_parts(allocation.0, length as usize) }.to_vec();
                            Ok(OwnedMaterializationFragment::Unicode(codepoints))
                        }
                        Err(failure) => Err(failure),
                    }
                })
                    .map_err(|error| converter_bridge.remember(error))
            }).collect()
        })));
        // Registered file subdocuments represent objects constructed before
        // rendering. Attach their parts before compilation or callbacks, and
        // reuse their body XML when the value is subsequently printed.
        if has_subdoc { core.render_init().map_err(error)?; }
        let mut subdocuments = Vec::new();
        if let Some(specials) = payload["specials"].as_array() {
            let mut registry = bridge.specials.lock().unwrap();
            for special in specials {
                if special["value"]["kind"].as_str() != Some("subdoc") { continue; }
                let identity = special["identity"].as_u64().ok_or_else(|| error("invalid special identity"))? as usize;
                if registry.contains_key(&identity) { continue; }
                let index = core.deferred.len();
                let value = context_value_with_provenance(&special["value"], &mut core).map_err(error)?;
                registry.insert(identity, value);
                core.deferred_by_oid.insert(identity, index);
                subdocuments.push(index);
            }
        }
        for index in subdocuments {
            let xml = core.materialize_one(docxtplrs::template::DOCUMENT_PART, index);
            if let Some(original) = bridge.take_error() { return Err(original); }
            core.attached_subdocuments.insert(index, xml.map_err(error)?);
        }
        // Create both environments before rendering. Their Python autoescape
        // callback follows the supplied Environment even for core properties.
        for autoescape in [false, true] {
            let mut env = docxtplrs::template::make_env(autoescape, &core);
            jinja::add_jinja2_compat(&mut env);
            python_bridge::configure(&mut env, &bridge)?;
            core.env_caches[usize::from(autoescape)] = Some(EnvCache {
                autoescape, r#gen: core.env_gen, env_options: core.env_options.clone(), env,
                tpl_hashes: HashMap::new(),
            });
        }
        let make_context = |core: &mut docxtplrs::template::TplCore, _part: &str| {
            let snapshot = if bridge.asynchronous {
                Python::attach(|py| bridge.opaque_value(py.None().bind(py)))
            } else {
                bridge.snapshot().map_err(|original| bridge.remember(original).to_string())?
            };
            if let Some(specials) = payload["specials"].as_array() {
                let mut registry = bridge.specials.lock().unwrap();
                for special in specials {
                    let identity = special["identity"].as_u64().ok_or("invalid special identity")? as usize;
                    if !registry.contains_key(&identity) {
                        if special["value"]["_lazy"].as_bool() == Some(true) {
                            bridge.lazy_images.lock().unwrap().insert(identity);
                        }
                        let index = core.deferred.len();
                        let value = context_value_with_provenance(&special["value"], core)?;
                        registry.insert(identity, value);
                        core.deferred_by_oid.insert(identity, index);
                    }
                }
            }
            Ok(snapshot)
        };
        let rendered = if has_subdoc { core.render_initialized(autoescape, &make_context) }
            else { core.render(autoescape, &make_context) };
        if let Some(original) = bridge.take_error() { return Err(original); }
        if let Some(bytes) = core.footnote_decode_error.take() {
            // SAFETY: the byte allocation remains live for the call and the
            // GIL is held. Strict decoding returns null with the original
            // CPython UnicodeDecodeError for these invalid bytes.
            return Err(Python::attach(|py| unsafe {
                let length = match isize::try_from(bytes.len()) {
                    Ok(length) => length,
                    Err(_) => return pyo3::exceptions::PyOverflowError::new_err("footnote exceeds Python's byte limit"),
                };
                let ptr = pyo3::ffi::PyUnicode_DecodeUTF8(bytes.as_ptr().cast(), length, std::ptr::null());
                if ptr.is_null() { PyErr::fetch(py) } else {
                    pyo3::ffi::Py_DecRef(ptr);
                    PyRuntimeError::new_err("expected invalid UTF-8 footnote")
                }
            }));
        }
        rendered.map_err(error)?;
        // A Python value materialized after the body's tables were fixed can
        // leave markup in it; docxtpl's fix_tables recovers the body with
        // lxml, so recover it the same way before checking it.
        let mut xml = core.get_xml();
        if !invalid_recovered.load(Ordering::Relaxed)
            && xml.as_ref().map_or(true, |xml| xml_check(xml.as_bytes()).is_err())
            && recover_rendered_document(&mut core).map_err(error)? {
            xml = core.get_xml();
        }
        let xml_valid = xml_check(xml.map_err(error)?.as_bytes());
        let raw_recovered = xml_valid.is_err() && invalid_recovered.load(Ordering::Relaxed);
        if !raw_recovered { xml_valid.map_err(error)?; }
        let mut bytes = if raw_recovered { save_recovered_body_compatible(&mut core) }
            else { save_rendered_compatible(&mut core) }.map_err(error)?;
        zip_metadata::normalize_permissions(&mut bytes).map_err(error)?;
        Ok((bytes, bridge.callbacks.load(std::sync::atomic::Ordering::Relaxed),
            bridge.formats.load(std::sync::atomic::Ordering::Relaxed),
            bridge.fast_formats.load(std::sync::atomic::Ordering::Relaxed)))
    })?;
    let execution = PyDict::new(py);
    execution.set_item("renderer", "native")?;
    execution.set_item("template_runtime", "MiniJinja+rsyi/jinja")?;
    execution.set_item("context_bridge", "CPython references")?;
    execution.set_item("python_callback_calls", callbacks)?;
    execution.set_item("python_formatter_calls", 0)?;
    execution.set_item("native_value_formatter_calls", formats)?;
    execution.set_item("native_string_formatter_calls", fast_formats)?;
    Ok((PyBytes::new(py, &bytes).unbind(), execution.unbind()))
}

#[pyfunction]
fn compose_docx(py: Python<'_>, documents: Vec<Vec<u8>>) -> PyResult<Py<PyBytes>> {
    let bytes = py.detach(move || {
        let mut it = documents.into_iter();
        let mut composer = docxtplrs::composer::Composer::new(it.next().ok_or("at least one document required")?)?;
        for bytes in it { composer.append(&bytes)?; }
        composer.save_bytes()
    }).map_err(error)?;
    Ok(PyBytes::new(py, &bytes).unbind())
}

#[pyfunction]
fn validate_xml(source: &[u8]) -> PyResult<()> {
    xml_check(source).map_err(PyValueError::new_err)
}

#[pyfunction]
fn patch_xml(source: &str) -> String { docxtplrs::patch::patch_xml(source).into_owned() }

/// Validate the input protocol used by CPython's string regular expressions.
fn regex_string<'a, 'py>(source: &'a Bound<'py, PyAny>) -> PyResult<&'a Bound<'py, PyString>> {
    let py = source.py();
    match source.cast::<PyString>() {
        Ok(source) => Ok(source),
        Err(_) => {
            // The reference regex accepts contiguous buffers, then rejects
            // mixing them with a string pattern; failed acquisition falls back
            // to the ordinary input-type error.
            let mut view = pyo3::ffi::Py_buffer::new();
            // SAFETY: source is live, view is initialized and the GIL is held.
            let status = unsafe { pyo3::ffi::PyObject_GetBuffer(source.as_ptr(), &mut view, pyo3::ffi::PyBUF_SIMPLE) };
            if status == 0 {
                // SAFETY: a successful acquisition owns this view.
                unsafe { pyo3::ffi::PyBuffer_Release(&mut view) };
                return Err(PyTypeError::new_err("cannot use a string pattern on a bytes-like object"));
            }
            // A failed buffer lookup sets a Python exception that the regex
            // implementation replaces with its input-type error.
            drop(PyErr::fetch(py));
            let name = source.get_type().name()?.to_str()?.to_owned();
            Err(PyTypeError::new_err(format!("expected string or bytes-like object, got '{name}'")))
        }
    }
}

#[pyfunction]
fn headers_footers_encoding<'py>(py: Python<'py>, source: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyString>> {
    let source = regex_string(source)?;
    static WORD_VERSION: std::sync::OnceLock<docxtpl_core::UnicodeWordVersion> = std::sync::OnceLock::new();
    let version = *WORD_VERSION.get_or_init(|| {
        let info = py.version_info();
        match (info.major, info.minor) {
            (3, 11) => docxtpl_core::UnicodeWordVersion::Python311,
            (3, 12) => docxtpl_core::UnicodeWordVersion::Python312,
            _ => docxtpl_core::UnicodeWordVersion::Unqualified,
        }
    });
    match source.to_str() {
        Ok(source) => Ok(PyString::new(py, docxtpl_core::headers_footers_encoding_with_unicode(source, version))),
        Err(error) if error.is_instance_of::<PyUnicodeEncodeError>(py) => {
            // The binding owns this CPython allocation until it has built the
            // Rust string; Python's GIL and source reference remain held.
            struct UnicodeCopy(*mut pyo3::ffi::Py_UCS4);
            impl Drop for UnicodeCopy {
                fn drop(&mut self) {
                    // SAFETY: AsUCS4Copy returns memory owned by PyMem_Free.
                    unsafe { pyo3::ffi::PyMem_Free(self.0.cast()) }
                }
            }
            // SAFETY: source is a live PyString and the GIL is held.
            let length = unsafe { pyo3::ffi::PyUnicode_GetLength(source.as_ptr()) };
            if length < 0 { return Err(PyErr::fetch(py)); }
            // SAFETY: same source/GIL guarantees; null means a Python error.
            let copy = UnicodeCopy(unsafe { pyo3::ffi::PyUnicode_AsUCS4Copy(source.as_ptr()) });
            if copy.0.is_null() { return Err(PyErr::fetch(py)); }
            // SAFETY: the returned copy contains length code points plus a
            // terminator; the guard frees it after this borrow ends.
            let codes = unsafe { std::slice::from_raw_parts(copy.0, length as usize) };
            let mut normalized = String::with_capacity(codes.len());
            for &code in codes { normalized.push(char::from_u32(code).unwrap_or('\u{fffd}')); }
            let matched = docxtpl_core::headers_footers_encoding_with_unicode(&normalized, version);
            let start = (matched.as_ptr() as usize).checked_sub(normalized.as_ptr() as usize);
            if let Some(start) = start.filter(|start| *start < normalized.len()) {
                let offset = normalized[..start].chars().count();
                let end = offset + matched.chars().count();
                // SAFETY: source is live, indices are code-point offsets, and
                // the returned owned reference is transferred to Bound.
                let result = unsafe { pyo3::ffi::PyUnicode_Substring(source.as_ptr(), offset as isize, end as isize) };
                return Ok(unsafe { Bound::from_owned_ptr_or_err(py, result) }?.cast_into::<PyString>()?);
            }
            Ok(PyString::new(py, matched))
        }
        Err(error) => Err(error),
    }
}

/// Unpack a two-item Python iterable with the reference error behavior.
fn unpack_two<'py>(item: &Bound<'py, PyAny>) -> PyResult<(Py<PyAny>, Py<PyAny>)> {
    let (first, second) = unpack_pair(item)?;
    Ok((first.unbind(), second.unbind()))
}

fn unpack_pair<'py>(pair: &Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyAny>,Bound<'py, PyAny>)> {
    let py=pair.py();
    let values=if pair.is_exact_instance_of::<PyTuple>() {
        let tuple=pair.cast::<PyTuple>()?;
        match tuple.len() {
            2 => (tuple.get_item(0)?,tuple.get_item(1)?),
            0|1 => return Err(pyo3::exceptions::PyValueError::new_err(format!("not enough values to unpack (expected 2, got {})",tuple.len()))),
            _ => return Err(pyo3::exceptions::PyValueError::new_err("too many values to unpack (expected 2)")),
        }
    } else {
        let mut values=match pair.try_iter() {
            Ok(values) => values,
            Err(error) => {
                let object_type=unsafe { pyo3::ffi::Py_TYPE(pair.as_ptr()) };
                let (iter,item)=unsafe {
                    (pyo3::ffi::PyType_GetSlot(object_type,pyo3::ffi::Py_tp_iter),
                     pyo3::ffi::PyType_GetSlot(object_type,pyo3::ffi::Py_sq_item))
                };
                if error.is_instance_of::<pyo3::exceptions::PyTypeError>(py) && iter.is_null() && item.is_null() {
                    let message = unsafe { Bound::from_owned_ptr_or_err(py,
                        pyo3::ffi::PyUnicode_FromFormat(c"cannot unpack non-iterable %.200s object".as_ptr(), (*object_type).tp_name))? };
                    return Err(PyErr::from_value(py.get_type::<PyTypeError>().call1((message,))?));
                }
                return Err(error);
            }
        };
        let key=values.next().transpose()?.ok_or_else(|| pyo3::exceptions::PyValueError::new_err("not enough values to unpack (expected 2, got 0)"))?;
        let value=values.next().transpose()?.ok_or_else(|| pyo3::exceptions::PyValueError::new_err("not enough values to unpack (expected 2, got 1)"))?;
        if values.next().transpose()?.is_some() { return Err(pyo3::exceptions::PyValueError::new_err("too many values to unpack (expected 2)")); }
        (key,value)
    };
    Ok(values)
}


/// Iterate the exposed Python relationship graph without copying its objects.
#[pyclass]
struct HeaderFooterIter {
    items: Option<Py<PyIterator>>,
    uri: Option<Py<PyAny>>,
}

#[pymethods]
impl HeaderFooterIter {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }

    // A normal Python generator ignores values sent to a bare `yield`.
    // `yield from` delegates non-None values here, so keep that behavior.
    fn send(&self, py: Python<'_>, _value: &Bound<'_, PyAny>) -> PyResult<(Py<PyAny>, Py<PyAny>)> {
        self.__next__(py)?.ok_or_else(|| PyStopIteration::new_err(()))
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.items)?;
        visit.call(&self.uri)
    }

    fn __clear__(&mut self) {
        self.items = None;
        self.uri = None;
    }

    fn __next__(&self, py: Python<'_>) -> PyResult<Option<(Py<PyAny>, Py<PyAny>)>> {
        let (Some(items), Some(uri)) = (&self.items, &self.uri) else { return Ok(None) };
        loop {
            let mut items = items.bind(py).clone();
            let item = match items.next() {
                Some(item) => item?,
                None => return Ok(None),
            };
            let (key, relation) = unpack_two(&item)?;
            let relation = relation.bind(py);
            if relation.getattr(pyo3::intern!(py, "reltype"))?.eq(uri.bind(py))?
                && relation.getattr(pyo3::intern!(py, "target_part"))?.getattr(pyo3::intern!(py, "blob"))?.is_truthy()? {
                return Ok(Some((key, relation.getattr(pyo3::intern!(py, "target_part"))?.unbind())));
            }
        }
    }
}

#[pyfunction]
fn header_footer_iter(tpl: &Bound<'_, PyAny>, uri: Py<PyAny>) -> PyResult<HeaderFooterIter> {
    let py = tpl.py();
    let items = tpl.getattr(pyo3::intern!(py, "docx"))?.getattr(pyo3::intern!(py, "_part"))?.getattr(pyo3::intern!(py, "rels"))?
        .call_method0(pyo3::intern!(py, "items"))?.try_iter()?.unbind();
    Ok(HeaderFooterIter { items: Some(items), uri: Some(uri) })
}

/// Lazy adapter over the caller's header/footer iterator and overridable API.
#[pyclass]
struct HeaderFooterBuildIter {
    tpl: Option<Py<PyAny>>,
    items: Option<Py<PyIterator>>,
    context: Option<Py<PyAny>>,
    environment: Option<Py<PyAny>>,
}

impl HeaderFooterBuildIter {
    fn next_item(&self, py: Python<'_>) -> PyResult<Option<(Py<PyAny>, Py<PyAny>)>> {
        let (Some(tpl), Some(items), Some(context), Some(environment)) =
            (&self.tpl, &self.items, &self.context, &self.environment) else { return Ok(None) };
        let mut items = items.bind(py).clone();
        let item = match items.next() { Some(item) => item?, None => return Ok(None) };
        let (key, part) = unpack_two(&item)?;
        let tpl = tpl.bind(py);
        let xml = tpl.call_method1(pyo3::intern!(py, "get_part_xml"), (part.bind(py),))?;
        let encoding = tpl.call_method1(pyo3::intern!(py, "get_headers_footers_encoding"), (&xml,))?;
        let patched = tpl.call_method1(pyo3::intern!(py, "patch_xml"), (xml,))?;
        let rendered = tpl.call_method1(pyo3::intern!(py, "render_xml_part"),
            (patched, part.bind(py), context.bind(py), environment.bind(py)))?;
        let encoded = rendered.call_method1(pyo3::intern!(py, "encode"), (encoding,))?;
        Ok(Some((key, encoded.unbind())))
    }
}

#[pymethods]
impl HeaderFooterBuildIter {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }
    fn __next__(&self, py: Python<'_>) -> PyResult<Option<(Py<PyAny>, Py<PyAny>)>> {
        match self.next_item(py) {
            Err(error) if error.is_instance_of::<PyStopIteration>(py) => {
                // PEP 479: an exception inside the generator is not exhaustion.
                let result = PyRuntimeError::new_err("generator raised StopIteration");
                result.set_cause(py, Some(error.clone_ref(py)));
                result.set_context(py, Some(error));
                Err(result)
            }
            result => result,
        }
    }
    fn send(&self, py: Python<'_>, _value: &Bound<'_, PyAny>) -> PyResult<(Py<PyAny>, Py<PyAny>)> {
        self.__next__(py)?.ok_or_else(|| PyStopIteration::new_err(()))
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.tpl)?;
        visit.call(&self.items)?;
        visit.call(&self.context)?;
        visit.call(&self.environment)
    }
    fn __clear__(&mut self) {
        self.tpl = None; self.items = None; self.context = None; self.environment = None;
    }
}

#[pyfunction]
fn build_header_footer_iter(tpl: &Bound<'_, PyAny>, context: Py<PyAny>, uri: &Bound<'_, PyAny>,
    environment: Py<PyAny>) -> PyResult<HeaderFooterBuildIter> {
    let py = tpl.py();
    let items = tpl.call_method1(pyo3::intern!(py, "get_headers_footers"), (uri,))?.try_iter()?.unbind();
    Ok(HeaderFooterBuildIter { tpl: Some(tpl.clone().unbind()), items: Some(items),
        context: Some(context), environment: Some(environment) })
}

/// Replace the body through the exposed document graph, preserving identity.
#[pyfunction]
fn map_python_tree(tpl: &Bound<'_, PyAny>, tree: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = tpl.py();
    let root = tpl.getattr(pyo3::intern!(py, "docx"))?.getattr(pyo3::intern!(py, "_element"))?;
    let body = root.getattr(pyo3::intern!(py, "body"))?;
    root.call_method1(pyo3::intern!(py, "replace"), (body, tree))?;
    Ok(())
}

/// Parse a part with the exposed model's native XML parser and serialize it
/// through the caller's overridable method.
#[pyfunction]
fn python_part_xml<'py>(tpl: &Bound<'py, PyAny>, part: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = tpl.py();
    let serialize = tpl.getattr(pyo3::intern!(py, "xml_to_string"))?;
    let blob = part.getattr(pyo3::intern!(py, "blob"))?;
    let tree = parse_opc_xml(&blob)?;
    serialize.call1((tree,))
}

fn parse_opc_xml<'py>(blob: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = blob.py();
    // The reference imports parse_xml once; its globals stay attached to the
    // original module even if a package attribute is subsequently rebound.
    let parser_module = opc_xml_module(py)?.bind(py);
    let etree = parser_module.getattr(pyo3::intern!(py, "etree"))?;
    let parser = parser_module.getattr(pyo3::intern!(py, "oxml_parser"))?;
    etree.call_method1(pyo3::intern!(py, "fromstring"), (blob, parser))
}

/// Serialize the exposed lxml tree using the same native serializer as docxtpl.
#[pyfunction]
fn python_xml_to_string<'py>(tree: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = tree.py();
    let etree = LXML_ETREE_MODULE.get_or_try_init(py, || {
        find_loaded_module(py, pyo3::intern!(py, "lxml.etree"))?
            .ok_or_else(|| PyRuntimeError::new_err("lxml is required for the exposed document model"))
    })?.bind(py);
    xml_binding::serialize(etree, tree)
}

/// Render an already-patched part through the native template session.
#[pyfunction]
fn render_python_xml_part<'py>(tpl: &Bound<'py, PyAny>, source: &Bound<'py, PyAny>, part: &Bound<'py, PyAny>,
    context: &Bound<'py, PyAny>, environment: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = tpl.py();
    let source = regex_string(source)?.to_str()?;
    let source = docxtplrs::template::add_paragraph_newlines(source);
    let source = docxtplrs::template::normalize_template_newlines(&source).into_owned();
    tpl.setattr(pyo3::intern!(py, "current_rendering_part"), part)?;
    let environment = if environment.is_truthy()? { environment.clone().unbind() } else { py.None() };
    let config = py.import("docxtpl_native.bridge")?.getattr("BridgeConfig")?.call1((environment, false))?;
    config.setattr(pyo3::intern!(py, "package_values_live"), true)?;
    let session = python_session::PythonRenderSession::new(config.unbind(), context.clone().unbind())?;
    let rendered = session.render(py, "word/__direct_part__.xml".to_owned(), source)?;
    let rendered = rendered.bind(py).extract::<String>()?;
    let rendered = docxtplrs::template::remove_paragraph_newlines(&rendered).into_owned();
    let rendered = docxtplrs::template::restore_escaped_delims(rendered);
    tpl.call_method1(pyo3::intern!(py, "resolve_listing"), (rendered,))
}

/// Build the body through the caller's overridable methods and live part.
#[pyfunction]
fn build_python_xml<'py>(tpl: &Bound<'py, PyAny>, context: &Bound<'py, PyAny>,
    environment: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = tpl.py();
    let xml = tpl.call_method0(pyo3::intern!(py, "get_xml"))?;
    let xml = tpl.call_method1(pyo3::intern!(py, "patch_xml"), (xml,))?;
    let render = tpl.getattr(pyo3::intern!(py, "render_xml_part"))?;
    let part = tpl.getattr(pyo3::intern!(py, "docx"))?.getattr(pyo3::intern!(py, "_part"))?;
    render.call1((xml, part, context, environment))
}

/// Render core properties in the exposed model, retaining setter order.
#[pyfunction]
fn render_exposed_properties(tpl: &Bound<'_, PyAny>, context: &Bound<'_, PyAny>,
    environment: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = tpl.py();
    let config = py.import("docxtpl_native.bridge")?.getattr("BridgeConfig")?.call1((environment, false))?;
    config.setattr(pyo3::intern!(py, "package_values_live"), true)?;
    let session = python_session::PythonRenderSession::new(config.unbind(), context.clone().unbind())?;
    for name in [pyo3::intern!(py, "author"), pyo3::intern!(py, "comments"),
        pyo3::intern!(py, "identifier"), pyo3::intern!(py, "language"),
        pyo3::intern!(py, "subject"), pyo3::intern!(py, "title")] {
        let value = tpl.getattr(pyo3::intern!(py, "docx"))?
            .getattr(pyo3::intern!(py, "core_properties"))?.getattr(name)?;
        let rendered = session.render(py, "<string>".to_owned(), value.extract::<String>()?)?;
        tpl.getattr(pyo3::intern!(py, "docx"))?
            .getattr(pyo3::intern!(py, "core_properties"))?.setattr(name, rendered)?;
    }
    Ok(())
}

/// Render footnotes on a caller-owned Python document graph.
#[pyfunction]
fn render_exposed_footnotes(tpl: &Bound<'_, PyAny>, context: &Bound<'_, PyAny>,
    environment: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = tpl.py();
    let environment = if environment.is_none() {
        let module = find_loaded_module(py, pyo3::intern!(py, "jinja2"))?
            .ok_or_else(|| PyRuntimeError::new_err("jinja2 is required for the exposed document model"))?;
        module.bind(py).getattr(pyo3::intern!(py, "Environment"))?.call0()?.unbind()
    } else { environment.clone().unbind() };
    // These are persistent non-interned constants in the reference function.
    static NOTE_TYPE: pyo3::sync::PyOnceLock<Py<PyString>> = pyo3::sync::PyOnceLock::new();
    static UTF8: pyo3::sync::PyOnceLock<Py<PyString>> = pyo3::sync::PyOnceLock::new();
    let note_type = NOTE_TYPE.get_or_init(py, || PyString::new(py,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml").unbind());
    let utf8 = UTF8.get_or_init(py, || PyString::new(py, "utf-8").unbind());
    let sections = tpl.getattr(pyo3::intern!(py, "docx"))?
        .getattr(pyo3::intern!(py, "sections"))?.try_iter()?;
    for section in sections {
        let section = section?;
        let parts = section.getattr(pyo3::intern!(py, "part"))?.getattr(pyo3::intern!(py, "package"))?
            .getattr(pyo3::intern!(py, "parts"))?.try_iter()?;
        for part in parts {
            let part = part?;
            if !part.getattr(pyo3::intern!(py, "content_type"))?.eq(note_type.bind(py))? { continue; }
            let patch = tpl.getattr(pyo3::intern!(py, "patch_xml"))?;
            let first_blob = part.getattr(pyo3::intern!(py, "blob"))?;
            let is_bytes = first_blob.is_instance(&py.get_type::<PyBytes>())?;
            let blob = part.getattr(pyo3::intern!(py, "blob"))?;
            let source = if is_bytes { blob.call_method1(pyo3::intern!(py, "decode"), (utf8.bind(py),))? } else { blob };
            let xml = patch.call1((source,))?;
            let rendered = tpl.call_method1(pyo3::intern!(py, "render_xml_part"),
                (xml, &part, context, environment.bind(py)))?;
            let bytes = rendered.call_method1(pyo3::intern!(py, "encode"), (utf8.bind(py),))?;
            part.setattr(pyo3::intern!(py, "_blob"), bytes)?;
        }
    }
    Ok(())
}

/// Replace a live header/footer part and copy its relationship graph.
#[pyfunction]
fn map_python_header_footer(tpl: &Bound<'_, PyAny>, key: &Bound<'_, PyAny>, xml: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = tpl.py();
    let old = tpl.getattr(pyo3::intern!(py, "docx"))?.getattr(pyo3::intern!(py, "_part"))?
        .getattr(pyo3::intern!(py, "rels"))?.get_item(key)?.getattr(pyo3::intern!(py, "target_part"))?;
    let class = xml_part_class(py)?.bind(py);
    let partname = old.getattr(pyo3::intern!(py, "partname"))?;
    let content_type = old.getattr(pyo3::intern!(py, "content_type"))?;
    let package = old.getattr(pyo3::intern!(py, "package"))?;
    let element = parse_opc_xml(xml)?;
    let new_part = class.call1((partname, content_type, element, package))?;
    let items = old.getattr(pyo3::intern!(py, "rels"))?.call_method0(pyo3::intern!(py, "items"))?.try_iter()?;
    for item in items {
        let item = item?;
        let (_, relation) = unpack_two(&item)?;
        let relation = relation.bind(py);
        let load = new_part.getattr(pyo3::intern!(py, "load_rel"))?;
        let kind = relation.getattr(pyo3::intern!(py, "reltype"))?;
        let target = relation.getattr(pyo3::intern!(py, "_target"))?;
        let id = relation.getattr(pyo3::intern!(py, "rId"))?;
        let external = relation.getattr(pyo3::intern!(py, "is_external"))?;
        load.call1((kind, target, id, external))?;
    }
    let relationship = tpl.getattr(pyo3::intern!(py, "docx"))?.getattr(pyo3::intern!(py, "_part"))?
        .getattr(pyo3::intern!(py, "rels"))?.get_item(key)?;
    relationship.setattr(pyo3::intern!(py, "_target"), new_part)?;
    Ok(())
}

/// Renumber docPr nodes on the exposed graph using the original object protocols.
#[pyfunction]
fn fix_python_docpr_ids(tpl: &Bound<'_, PyAny>, tree: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = tpl.py();
    let xpath = tree.getattr(pyo3::intern!(py, "xpath"))?;
    let namespaces = docx_module(py)?.bind(py).getattr(pyo3::intern!(py, "oxml"))?
        .getattr(pyo3::intern!(py, "ns"))?.getattr(pyo3::intern!(py, "nsmap"))?;
    let kwargs = PyDict::new(py);
    kwargs.set_item(pyo3::intern!(py, "namespaces"), namespaces)?;
    // The reference uses one persistent non-interned code constant here.
    static DOC_PR_XPATH: pyo3::sync::PyOnceLock<Py<PyString>> = pyo3::sync::PyOnceLock::new();
    let expression = DOC_PR_XPATH.get_or_init(py, || PyString::new(py, "//wp:docPr").unbind());
    let elements = xpath.call((expression.bind(py),), Some(&kwargs))?;
    let one = 1i32.into_pyobject(py)?;
    for element in elements.try_iter()? {
        let element = element?;
        let index = tpl.getattr(pyo3::intern!(py, "docx_ids_index"))?;
        // SAFETY: both references are live Python objects and the GIL is held;
        // PyNumber_InPlaceAdd returns an owned reference or a Python exception.
        let next = unsafe { pyo3::ffi::PyNumber_InPlaceAdd(index.as_ptr(), one.as_ptr()) };
        let next = unsafe { Bound::from_owned_ptr_or_err(py, next) }?;
        tpl.setattr(pyo3::intern!(py, "docx_ids_index"), next)?;
        let value = tpl.getattr(pyo3::intern!(py, "docx_ids_index"))?.str()?;
        element.getattr(pyo3::intern!(py, "attrib"))?.set_item(pyo3::intern!(py, "id"), value)?;
    }
    Ok(())
}

/// Owned native source graph for relationships created before rendering.
#[pyclass]
struct HyperlinkSource { hyperlinks: opc::Hyperlinks }

#[pymethods]
impl HyperlinkSource {
    #[new]
    fn new(source: &[u8]) -> PyResult<Self> {
        Ok(Self { hyperlinks: opc::Hyperlinks::new(source).map_err(error)? })
    }
    fn relate(&mut self, url: &str) -> String { self.hyperlinks.relate(url) }
    fn to_bytes(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        let bytes = self.hyperlinks.to_bytes().map_err(error)?;
        Ok(PyBytes::new(py, &bytes).unbind())
    }
}

#[pyfunction]
fn crc32_bytes(source: &[u8]) -> u32 { docxtplrs::package::crc32(source) }


/// Recognize the conservative data-only Jinja subset used by the fast path.
/// Any uncertain syntax goes through the full compatibility frontend.
#[pyfunction]
fn simple_source_accepts(source: &str, names: &Bound<'_, PyAny>) -> PyResult<bool> {
    scan_simple_source(source, names, true)
}

/// The byte-level check of `simple_source_accepts`. With `statements`, a
/// source that has statements or outputs beyond a bare path goes to the AST
/// check; without, it is refused (the AST check reads single paths so).
fn scan_simple_source(source: &str, names: &Bound<'_, PyAny>, statements: bool) -> PyResult<bool> {
    const KEYWORDS: &[&str] = &[
        "False", "None", "True", "and", "as", "assert", "async", "await", "break",
        "class", "continue", "def", "del", "elif", "else", "except", "finally",
        "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal",
        "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
        "true", "false", "none", "self", "super", "loop", "caller",
    ];
    let bytes = source.as_bytes();
    let mut i = 0;
    let mut remaining = Vec::with_capacity(bytes.len());
    while i < bytes.len() {
        if bytes[i..].starts_with(b"{{") {
            let mut cursor = i + 2;
            while cursor < bytes.len() && matches!(bytes[cursor], b' ' | b'\t' | b'\r' | b'\n') { cursor += 1; }
            let start = cursor;
            if cursor < bytes.len() && (bytes[cursor].is_ascii_alphabetic() || bytes[cursor] == b'_') {
                cursor += 1;
                while cursor < bytes.len() && (bytes[cursor].is_ascii_alphanumeric() || bytes[cursor] == b'_') { cursor += 1; }
                let end = cursor;
                let mut valid = true;
                loop {
                    while cursor < bytes.len() && matches!(bytes[cursor], b' ' | b'\t' | b'\r' | b'\n') { cursor += 1; }
                    if bytes.get(cursor) == Some(&b'.') {
                        cursor += 1;
                        while cursor < bytes.len() && matches!(bytes[cursor], b' ' | b'\t' | b'\r' | b'\n') { cursor += 1; }
                        let start = cursor;
                        if !bytes.get(cursor).is_some_and(|ch| ch.is_ascii_alphabetic() || *ch == b'_') { valid = false; break; }
                        cursor += 1;
                        while cursor < bytes.len() && (bytes[cursor].is_ascii_alphanumeric() || bytes[cursor] == b'_') { cursor += 1; }
                        if KEYWORDS.contains(&&source[start..cursor]) { valid = false; break; }
                    } else if bytes.get(cursor) == Some(&b'[') {
                        cursor += 1;
                        while cursor < bytes.len() && matches!(bytes[cursor], b' ' | b'\t' | b'\r' | b'\n') { cursor += 1; }
                        if matches!(bytes.get(cursor), Some(b'\'' | b'"')) {
                            let quote = bytes[cursor];
                            cursor += 1;
                            while cursor < bytes.len() && bytes[cursor] != quote {
                                if !(b' '..=b'~').contains(&bytes[cursor]) || matches!(bytes[cursor], b'\\' | b'<' | b'>' | b'&') { break; }
                                cursor += 1;
                            }
                            if bytes.get(cursor) != Some(&quote) { valid = false; break; }
                            cursor += 1;
                        } else {
                            if bytes.get(cursor) == Some(&b'-') { cursor += 1; }
                            let start = cursor;
                            while cursor < bytes.len() && bytes[cursor].is_ascii_digit() { cursor += 1; }
                            if cursor == start || cursor - start > 9
                                || (cursor - start > 1 && bytes[start] == b'0') { valid = false; break; }
                        }
                        while cursor < bytes.len() && matches!(bytes[cursor], b' ' | b'\t' | b'\r' | b'\n') { cursor += 1; }
                        if bytes.get(cursor) != Some(&b']') { valid = false; break; }
                        cursor += 1;
                    } else {
                        break;
                    }
                }
                if valid && bytes[cursor..].starts_with(b"}}") {
                    let name = &source[start..end];
                    if KEYWORDS.contains(&name) || !names.call_method1("__contains__", (name,))?.is_truthy()? {
                        // A statement may bind the name (a loop target, `loop`),
                        // or it is a literal (`true`, `none`): the AST check decides.
                        if statements { return simple_conditional_source_accepts(source, names); }
                        return Ok(false);
                    }
                    i = cursor + 2;
                    continue;
                }
            }
        }
        remaining.push(bytes[i]);
        i += 1;
    }
    // Statements, or outputs beyond a bare path, go to the AST check.
    if statements && remaining.windows(2).any(|pair| pair == b"{%" || pair == b"{{") {
        return simple_conditional_source_accepts(source, names);
    }
    Ok(!remaining.windows(2).any(|pair| pair[0] == b'{' && matches!(pair[1], b'{' | b'%' | b'#')))
}

/// Reuse the native parser, accepting only conditionals and plain `for`
/// loops over the same closed path grammar as interpolation. Calls and
/// compilation callbacks stay on the complete compatibility frontend.
fn simple_conditional_source_accepts(source: &str, names: &Bound<'_, PyAny>) -> PyResult<bool> {
    use minijinja::machinery::{ast, parse};
    // `loop` attributes Jinja's LoopContext exposes as plain values.
    const LOOP_ATTRIBUTES: &[&str] = &["index", "index0", "revindex", "revindex0", "first", "last",
        "length", "depth", "depth0", "previtem", "nextitem"];
    if source.contains("{#") || ["{{-", "{{+", "{%-", "{%+", "-%}", "+%}"].iter().any(|s| source.contains(s)) {
        return Ok(false);
    }
    // Standard filters applied to a path with literal arguments. Their input
    // is never constant, so Jinja folds nothing at compile time.
    // (name, most positional literal arguments)
    const FILTERS: &[(&str, usize)] = &[("upper", 0), ("lower", 0), ("title", 0), ("capitalize", 0), ("trim", 0),
        ("default", 2), ("d", 2), ("length", 0), ("count", 0), ("join", 1), ("first", 0), ("last", 0),
        ("string", 0), ("e", 0), ("escape", 0)];
    // Standard tests that take no argument.
    const TESTS: &[&str] = &["defined", "undefined", "none"];
    struct Scope<'s, 'py> { names: Bound<'py, PyAny>, in_loop: bool, source: &'s str }
    /// A literal both parsers read the same way: a short printable ASCII
    /// string without escapes, a small decimal integer, a boolean or none.
    fn literal(scope: &Scope<'_, '_>, value: &ast::Expr<'_>) -> bool {
        let ast::Expr::Const(_) = value else { return false; };
        let span = value.span();
        let Some(text) = scope.source.get(span.start_offset as usize..span.end_offset as usize) else { return false; };
        let bytes = text.as_bytes();
        match bytes.first() {
            Some(quote @ (b'\'' | b'"')) => bytes.len() >= 2 && bytes.len() <= 202 && bytes[bytes.len() - 1] == *quote
                && bytes[1..bytes.len() - 1].iter().all(|ch| (b' '..=b'~').contains(ch) && !matches!(*ch, b'\\' | b'\'' | b'"' | b'<' | b'>' | b'&')),
            Some(b'0'..=b'9') => bytes.len() <= 9 && bytes.iter().all(u8::is_ascii_digit) && (bytes.len() == 1 || bytes[0] != b'0'),
            _ => matches!(text, "true" | "false" | "none" | "True" | "False" | "None"),
        }
    }
    /// A path, `loop` attribute or filtered value: not an operation.
    fn operand(scope: &Scope<'_, '_>, value: &ast::Expr<'_>) -> PyResult<bool> {
        match value {
            ast::Expr::Var(_) | ast::Expr::GetAttr(_) | ast::Expr::GetItem(_) | ast::Expr::Filter(_)
                | ast::Expr::Call(_) => expression(scope, value, false),
            _ => Ok(false),
        }
    }
    // Literal-only operations may fold at compile time in Jinja. In this
    // grammar they are standard and pure (no `finalize`, default filters),
    // so folding and evaluating at runtime give the same value or error and
    // run no user code.
    fn operands<'e, 'a: 'e>(scope: &Scope<'_, '_>, values: impl IntoIterator<Item = &'e ast::Expr<'a>>) -> PyResult<bool> {
        for value in values {
            if !(literal(scope, value) || operand(scope, value)?) { return Ok(false); }
        }
        Ok(true)
    }
    fn expression(scope: &Scope<'_, '_>, value: &ast::Expr<'_>, condition: bool) -> PyResult<bool> {
        match value {
            ast::Expr::UnaryOp(op) if condition && matches!(op.op, ast::UnaryOpKind::Not) => expression(scope, &op.expr, true),
            ast::Expr::BinOp(op) if condition && matches!(op.op, ast::BinOpKind::ScAnd | ast::BinOpKind::ScOr) => {
                Ok(expression(scope, &op.left, true)? && expression(scope, &op.right, true)?)
            }
            // A comparison of a value with a value or literal; not two literals.
            ast::Expr::BinOp(op) if condition && matches!(op.op, ast::BinOpKind::Eq | ast::BinOpKind::Ne
                | ast::BinOpKind::Lt | ast::BinOpKind::Lte | ast::BinOpKind::Gt | ast::BinOpKind::Gte | ast::BinOpKind::In) => {
                Ok((literal(scope, &op.left) || expression(scope, &op.left, false)?)
                    && (literal(scope, &op.right) || expression(scope, &op.right, false)?))
            }
            // Arithmetic or `~` between values and literals, never only
            // literals: Jinja folds nothing, and both paths evaluate it at
            // runtime. Operands are not themselves operations, so operator
            // precedence cannot differ between the parsers.
            ast::Expr::BinOp(op) if matches!(op.op, ast::BinOpKind::Add | ast::BinOpKind::Sub | ast::BinOpKind::Mul
                | ast::BinOpKind::Div | ast::BinOpKind::FloorDiv | ast::BinOpKind::Rem) => {
                operands(scope, [&op.left, &op.right])
            }
            ast::Expr::Concat(concat) => operands(scope, concat.values.iter()),
            // A call of a path with positional literal or value arguments.
            // Jinja never folds a call; it runs at runtime on both paths
            // (and, as a callback, turns the frontend off for later parts).
            ast::Expr::Call(call) => {
                if !matches!(&call.expr, ast::Expr::Var(_) | ast::Expr::GetAttr(_) | ast::Expr::GetItem(_))
                    || !expression(scope, &call.expr, false)? {
                    return Ok(false);
                }
                for arg in &call.args {
                    let ast::CallArg::Pos(value) = arg else { return Ok(false); };
                    if !(literal(scope, value) || operand(scope, value)?) { return Ok(false); }
                }
                Ok(true)
            }
            ast::Expr::IfExpr(choice) => {
                let Some(otherwise) = &choice.false_expr else { return Ok(false); };
                Ok(expression(scope, &choice.test_expr, true)?
                    && (literal(scope, &choice.true_expr) || operand(scope, &choice.true_expr)?)
                    && (literal(scope, otherwise) || operand(scope, otherwise)?))
            }
            ast::Expr::Test(test) if condition && TESTS.contains(&test.name) && test.args.is_empty() => {
                expression(scope, &test.expr, false)
            }
            ast::Expr::Filter(filter) if FILTERS.iter().any(|(name, most)| *name == filter.name && filter.args.len() <= *most) => {
                let Some(input) = &filter.expr else { return Ok(false); };
                Ok(filter.args.iter().all(|arg| matches!(arg, ast::CallArg::Pos(value) if literal(scope, value)))
                    && (literal(scope, input) || expression(scope, input, false)?))
            }
            // `loop.<attribute>` of the innermost loop.
            ast::Expr::GetAttr(attr) if scope.in_loop && matches!(&attr.expr, ast::Expr::Var(var) if var.id == "loop") => {
                Ok(LOOP_ATTRIBUTES.contains(&attr.name))
            }
            ast::Expr::Const(_) => Ok(literal(scope, value)),
            ast::Expr::Var(_) | ast::Expr::GetAttr(_) | ast::Expr::GetItem(_) => {
                let span = value.span();
                let Some(text) = scope.source.get(span.start_offset as usize..span.end_offset as usize) else { return Ok(false); };
                scan_simple_source(&format!("{{{{ {text} }}}}"), &scope.names, false)
            }
            _ => Ok(false),
        }
    }
    /// A name the path grammar can read back, other than `loop` and the
    /// names Jinja reserves.
    fn valid_name(id: &str) -> bool {
        id.bytes().next().is_some_and(|ch| ch.is_ascii_alphabetic() || ch == b'_')
            && id.bytes().all(|ch| ch.is_ascii_alphanumeric() || ch == b'_')
            && !matches!(id, "loop" | "caller" | "self" | "super" | "true" | "false" | "none"
                | "True" | "False" | "None")
    }
    /// A loop target: a name or a tuple of names that Jinja may assign.
    fn targets<'a>(value: &ast::Expr<'a>, into: &mut Vec<&'a str>) -> bool {
        match value {
            ast::Expr::Var(var) => {
                into.push(var.id);
                valid_name(var.id)
            }
            ast::Expr::List(list) => !list.items.is_empty() && list.items.iter().all(|item| targets(item, into)),
            _ => false,
        }
    }
    // `scope` gains the names a `set` assigns, for the statements after it
    // (an `if` does not scope them; a loop body keeps its own).
    fn statement(scope: &mut Scope<'_, '_>, value: &ast::Stmt<'_>) -> PyResult<bool> {
        let children = match value {
            ast::Stmt::Template(node) => &node.children,
            ast::Stmt::EmitRaw(_) => return Ok(true),
            ast::Stmt::EmitExpr(node) => return expression(scope, &node.expr, false),
            ast::Stmt::Set(node) => {
                let mut bound = Vec::new();
                if !matches!(&node.target, ast::Expr::Var(_)) || !targets(&node.target, &mut bound)
                    || !(literal(scope, &node.expr) || expression(scope, &node.expr, false)?) {
                    return Ok(false);
                }
                scope.names = scope.names.call_method1("union", (bound,))?;
                return Ok(true);
            }
            // A macro with name parameters and literal defaults, whose body is
            // in the grammar with the parameters as known names. Its name is
            // known after the definition, so it can be called.
            ast::Stmt::Macro(node) => {
                let mut parameters = Vec::new();
                if !node.args.iter().all(|arg| matches!(arg, ast::Expr::Var(_)) && targets(arg, &mut parameters))
                    || parameters.iter().any(|name| matches!(*name, "varargs" | "kwargs"))
                    || (1..parameters.len()).any(|index| parameters[..index].contains(&parameters[index]))
                    || !node.defaults.iter().all(|value| literal(scope, value)) {
                    return Ok(false);
                }
                if !valid_name(node.name) || matches!(node.name, "varargs" | "kwargs") { return Ok(false); }
                let name = vec![node.name];
                let mut inner = Scope { names: scope.names.call_method1("union", (parameters,))?, in_loop: false,
                    source: scope.source };
                for child in &node.body {
                    if !statement(&mut inner, child)? { return Ok(false); }
                }
                scope.names = scope.names.call_method1("union", (name,))?;
                return Ok(true);
            }
            ast::Stmt::IfCond(node) => {
                if !expression(scope, &node.expr, true)? { return Ok(false); }
                for child in &node.true_body {
                    if !statement(scope, child)? { return Ok(false); }
                }
                &node.false_body
            }
            ast::Stmt::ForLoop(node) => {
                if node.filter_expr.is_some() || node.recursive || !expression(scope, &node.iter, false)? {
                    return Ok(false);
                }
                let mut bound = Vec::new();
                if !targets(&node.target, &mut bound) { return Ok(false); }
                let names = scope.names.call_method1("union", (bound,))?;
                let mut inner = Scope { names, in_loop: true, source: scope.source };
                for child in &node.body {
                    if !statement(&mut inner, child)? { return Ok(false); }
                }
                // The else body runs in the enclosing scope.
                &node.else_body
            }
            _ => return Ok(false),
        };
        for child in children {
            if !statement(scope, child)? { return Ok(false); }
        }
        Ok(true)
    }
    let Ok(tree) = parse(source, "<string>", Default::default(), Default::default()) else { return Ok(false); };
    statement(&mut Scope { names: names.clone(), in_loop: false, source }, &tree)
}

/// Escape listing text using the reference html.escape(text, quote=True) rules.
#[pyfunction]
fn escape_listing(py: Python<'_>, input: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    // The reference preserves str subclasses and bytes before calling replace;
    // all other objects go through Python's __str__ protocol first.
    let text = if input.is_instance_of::<PyString>() || input.is_instance_of::<PyBytes>() {
        input.clone()
    } else {
        input.str()?.into_any()
    };
    escape_html_text(py, &text)
}

/// Escape an already-converted value, preserving replace() protocols and
/// unpaired surrogates without invoking the Python html.escape algorithm.
fn escape_html_text(py: Python<'_>, text: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    if text.is_exact_instance_of::<PyString>() {
        match text.extract::<&str>() {
            Ok(value) => {
                // CPython's repeated str.replace calls retain an unchanged
                // exact string. Preserve that identity without a new output.
                if !value.bytes().any(|byte| matches!(byte, b'&' | b'<' | b'>' | b'"' | b'\'')) {
                    return Ok(text.clone().unbind());
                }
                let escaped = docxtplrs::richtext::listing_xml(value);
                return Ok(PyString::new(py, &escaped).into_any().unbind());
            }
            Err(error) if error.is_instance_of::<PyUnicodeEncodeError>(py) => {}
            Err(error) => return Err(error),
        }
    }
    // str subclasses can override replace(), and Python strings may contain
    // unpaired surrogates. Call their protocols in the reference order.
    let mut escaped = text.clone();
    for (from, to) in [("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"),
                       ("\"", "&quot;"), ("'", "&#x27;")] {
        escaped = escaped.call_method1("replace", (from, to))?;
    }
    Ok(escaped.unbind())
}


/// Build a rich-text run for the ordinary Python-compatible value subset.
/// Unusual Python objects are handled by the compatibility adapter.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn python_richtext_run(
    text: &str, style: Option<String>, color: Option<String>,
    highlight: Option<String>, size: Option<u32>, subscript: bool,
    superscript: bool, bold: bool, italic: bool, underline: Option<String>,
    strike: bool, font: Option<String>, url_id: Option<String>, rtl: bool, lang: Option<String>,
) -> String {
    let props = docxtplrs::richtext::TextProps {
        style, color, highlight, size, subscript, superscript, bold, italic,
        underline, strike, font, url_id, rtl, lang,
    };
    docxtplrs::richtext::richtext_run(text, &props)
}

/// Construct a paragraph around previously generated run XML.
#[pyfunction]
fn python_richtext_paragraph(runs_xml: &str, parastyle: Option<&str>) -> String {
    docxtplrs::richtext::richtext_paragraph(runs_xml, parastyle.filter(|s| !s.is_empty()))
}

mod fix_tables;
mod preprocessing;
mod crc_binding;
mod postprocessing;
mod zip_metadata;
mod zip_lifecycle;
mod composer;
mod replacements;
mod document_api;
mod exposed_save;
mod analysis_api;
mod pyglobals;
mod value_binding;
mod read_binding;
mod buffer_binding;
mod live_routing;

// Capture the already-loaded C provider without invoking a Python callback.
// A render callback may later replace the mutable C-API registry.
fn loaded_lxml_provider_path(py: Python<'_>) -> Option<std::path::PathBuf> {
    let module = LXML_ETREE_MODULE.get(py)?.bind(py).cast::<PyModule>().ok()?;
    if let Some(capi) = module.dict().get_item("__pyx_capi__").ok()? {
        if let Ok(capi) = capi.cast::<PyDict>() {
            if capi.is_exact_instance_of::<PyDict>() {
                if let Some(capsule) = capi.get_item("rootNodeOrRaise").ok()? {
                    let signature = c"struct LxmlElement *(PyObject *)";
                    if unsafe { pyo3::ffi::PyCapsule_IsValid(capsule.as_ptr(), signature.as_ptr()) } == 1 {
                        let address = unsafe { pyo3::ffi::PyCapsule_GetPointer(capsule.as_ptr(), signature.as_ptr()) };
                        if !address.is_null() {
                            if let Ok(path) = xml_binding::provider_path(module.as_any(), address) { return Some(path); }
                        }
                    }
                }
            }
        }
    }
    #[cfg(unix)]
    { lxml_type_provider_path(module) }
    // On non-Unix targets provider_path cannot independently map a C address
    // to its owner, so it cannot establish the provider with type slots.
    #[cfg(not(unix))]
    { None }
}

// The mutable capsule registry can be replaced before extension loading.
// Two independent C type slots must identify the already-loaded module's
// own file before it is used as the direct libxml provider.
#[cfg(unix)]
fn lxml_type_provider_path(module: &Bound<'_, PyModule>) -> Option<std::path::PathBuf> {
    let file = module.dict().get_item("__file__").ok()??;
    if !file.is_exact_instance_of::<PyString>() { return None; }
    let file = std::path::PathBuf::from(file.extract::<String>().ok()?);
    for name in ["_Element", "XMLParser"] {
        let object = module.dict().get_item(name).ok()??;
        if !object.is_exact_instance_of::<pyo3::types::PyType>() { return None; }
        let class = object.cast::<pyo3::types::PyType>().ok()?;
        let class_name = class.getattr("__name__").ok()?;
        let class_module = class.getattr("__module__").ok()?;
        if !class_name.is_exact_instance_of::<PyString>() || !class_module.is_exact_instance_of::<PyString>()
            || class_name.extract::<String>().ok()? != name || class_module.extract::<String>().ok()? != "lxml.etree" {
            return None;
        }
        let address = unsafe { pyo3::ffi::PyType_GetSlot(class.as_type_ptr(), pyo3::ffi::Py_tp_new) };
        if address.is_null() {
            unsafe { pyo3::ffi::PyErr_Clear() };
            return None;
        }
        if xml_binding::provider_path(module.as_any(), address).ok()? != file { return None; }
    }
    Some(file)
}

fn recover_lxml_unicode(py: Python<'_>, source: &Bound<'_, PyString>) -> PyResult<String> {
    recover_lxml_unicode_with_path(py, source, None)
}

fn recover_lxml_unicode_with_path(py: Python<'_>, source: &Bound<'_, PyString>, provider: Option<&std::path::Path>) -> PyResult<String> {
    let module = LXML_ETREE_MODULE.get_or_try_init(py, || {
        find_loaded_module(py, pyo3::intern!(py, "lxml.etree"))?
            .ok_or_else(|| PyRuntimeError::new_err("lxml is required for Unicode XML recovery"))
    })?.bind(py);
    let path = match provider {
        Some(path) => path.to_owned(),
        None => {
            let capsule = module.getattr("__pyx_capi__")?.get_item("rootNodeOrRaise")?;
            let address = unsafe { pyo3::ffi::PyCapsule_GetPointer(capsule.as_ptr(), c"struct LxmlElement *(PyObject *)".as_ptr()) };
            if address.is_null() { return Err(PyErr::fetch(py)); }
            xml_binding::provider_path(module, address)?
        }
    };
    let serializer = unsafe { docxtpl_core::foreign_xml::Serializer::load(path) }.map_err(error)?;
    let length = unsafe { pyo3::ffi::PyUnicode_GetLength(source.as_ptr()) };
    if length < 0 { return Err(PyErr::fetch(py)); }
    let allocation = unsafe { pyo3::ffi::PyUnicode_AsUCS4Copy(source.as_ptr()) };
    if allocation.is_null() { return Err(PyErr::fetch(py)); }
    struct UnicodeAllocation(*mut pyo3::ffi::Py_UCS4);
    impl Drop for UnicodeAllocation {
        fn drop(&mut self) { unsafe { pyo3::ffi::PyMem_Free(self.0.cast()) } }
    }
    let allocation = UnicodeAllocation(allocation);
    let codepoints = unsafe { std::slice::from_raw_parts(allocation.0, length as usize) };
    let kind = unsafe { pyo3::ffi::PyUnicode_KIND(source.as_ptr()) };
    // UTF-8 and Latin-1 encode ASCII identically, regardless of CPython's
    // internal ASCII flag; inspecting code points also works on Python 3.14.
    let ascii = kind == 1 && codepoints.iter().all(|&code| code <= 0x7f);
    let bytes = serializer.recover_unicode(codepoints, kind, ascii).map_err(error)?;
    String::from_utf8(bytes).map_err(error)
}

/// Laboratory entry point for differential checks of the C provider.
#[pyfunction]
fn _lab_recover_lxml_unicode(py: Python<'_>, source: &Bound<'_, PyString>) -> PyResult<String> {
    recover_lxml_unicode(py, source)
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(_lab_recover_lxml_unicode, module)?)?;
    python_bridge::capture_image_type(module.py())?;
    native_filters::initialize(module.py())?;
    native_filters::register(module)?;
    native_join::register(module)?;
    native_tests::initialize(module.py())?;
    constant_eval::initialize(module.py())?;
    module.add_function(wrap_pyfunction!(constant_eval::constant_value, module)?)?;
    module.add_function(wrap_pyfunction!(python_ast::requires_foreign_literals, module)?)?;
    module.add_class::<zip_lifecycle::ZipTimestampWriter>()?;
    composer::capture_loaded_dependencies(module.py())?;
    inline_image::capture_globals(module.py())?;
    if let Some(docx) = find_loaded_docx(module.py())? {
        let _ = DOCX_MODULE.set(module.py(), docx);
    }
    if let Some(opc) = find_loaded_module(module.py(), pyo3::intern!(module.py(), "docx.opc.oxml"))? {
        let _ = OPC_XML_MODULE.set(module.py(), opc);
    }
    if let Some(etree) = find_loaded_module(module.py(), pyo3::intern!(module.py(), "lxml.etree"))? {
        xml_binding::initialize(etree.bind(module.py()))?;
        let _ = LXML_ETREE_MODULE.set(module.py(), etree);
        if let Some(path) = loaded_lxml_provider_path(module.py()) {
            let _ = LXML_PROVIDER_PATH.set(module.py(), path);
        }
    }
    module.add_function(wrap_pyfunction!(process_docx, module)?)?;
    module.add_function(wrap_pyfunction!(process_python_docx, module)?)?;
    module.add_function(wrap_pyfunction!(zip_metadata::refresh_timestamps, module)?)?;
    module.add_class::<python_session::PythonRenderSession>()?;
    module.add_class::<native_package::NativePackage>()?;
    python_async::register(module)?;
    module.add_function(wrap_pyfunction!(python_operators::python_operator, module)?)?;
    module.add_function(wrap_pyfunction!(python_operators::python_concat, module)?)?;
    module.add_function(wrap_pyfunction!(python_format::python_format_value, module)?)?;
    module.add_function(wrap_pyfunction!(python_call::python_invoke, module)?)?;
    module.add_function(wrap_pyfunction!(python_context::python_context, module)?)?;
    module.add_function(wrap_pyfunction!(python_context::python_new_context, module)?)?;
    python_blocks::register(module)?;
    module.add_function(wrap_pyfunction!(python_context::python_eval_context, module)?)?;
    module.add_function(wrap_pyfunction!(python_context_helpers::python_context_get_all, module)?)?;
    module.add_function(wrap_pyfunction!(python_context_helpers::python_context_exported_body, module)?)?;
    module.add_function(wrap_pyfunction!(python_context_helpers::python_context_derived, module)?)?;
    module.add_function(wrap_pyfunction!(python_lookup::python_lookup, module)?)?;
    module.add_function(wrap_pyfunction!(python_sandbox::python_sandbox_call, module)?)?;
    module.add_function(wrap_pyfunction!(python_sandbox::python_is_safe_callable, module)?)?;
    module.add_function(wrap_pyfunction!(python_sandbox::python_is_safe_attribute, module)?)?;
    module.add_function(wrap_pyfunction!(python_session::finish_python_body, module)?)?;
    module.add_function(wrap_pyfunction!(python_session::resolve_listing_xml, module)?)?;
    module.add_function(wrap_pyfunction!(compose_docx, module)?)?;
    module.add_function(wrap_pyfunction!(validate_xml, module)?)?;
    module.add_function(wrap_pyfunction!(patch_xml, module)?)?;
    module.add_function(wrap_pyfunction!(headers_footers_encoding, module)?)?;
    module.add_function(wrap_pyfunction!(header_footer_iter, module)?)?;
    module.add_function(wrap_pyfunction!(build_header_footer_iter, module)?)?;
    module.add_function(wrap_pyfunction!(map_python_tree, module)?)?;
    module.add_function(wrap_pyfunction!(python_part_xml, module)?)?;
    module.add_function(wrap_pyfunction!(python_xml_to_string, module)?)?;
    module.add_function(wrap_pyfunction!(render_python_xml_part, module)?)?;
    module.add_function(wrap_pyfunction!(build_python_xml, module)?)?;
    module.add_function(wrap_pyfunction!(render_exposed_properties, module)?)?;
    module.add_function(wrap_pyfunction!(render_exposed_footnotes, module)?)?;
    module.add_function(wrap_pyfunction!(map_python_header_footer, module)?)?;
    module.add_function(wrap_pyfunction!(fix_python_docpr_ids, module)?)?;
    module.add_function(wrap_pyfunction!(fix_tables::fix_exposed_tables, module)?)?;
    module.add_function(wrap_pyfunction!(preprocessing::pre_process, module)?)?;
    module.add_function(wrap_pyfunction!(preprocessing::replace_pics, module)?)?;
    module.add_function(wrap_pyfunction!(preprocessing::replace_part_pics, module)?)?;
    module.add_class::<HeaderFooterIter>()?;
    module.add_function(wrap_pyfunction!(crc32_bytes, module)?)?;
    module.add_function(wrap_pyfunction!(crc_binding::crc_file_source, module)?)?;
    module.add_function(wrap_pyfunction!(postprocessing::post_process, module)?)?;
    module.add_function(wrap_pyfunction!(composer::init_composer, module)?)?;
    module.add_function(wrap_pyfunction!(replacements::replace_media, module)?)?;
    module.add_function(wrap_pyfunction!(replacements::replace_pic, module)?)?;
    module.add_function(wrap_pyfunction!(replacements::replace_embedded, module)?)?;
    module.add_function(wrap_pyfunction!(replacements::replace_zipname, module)?)?;
    module.add_function(wrap_pyfunction!(document_api::render_init, module)?)?;
    module.add_function(wrap_pyfunction!(document_api::save_exposed_model, module)?)?;
    module.add_function(wrap_pyfunction!(document_api::get_xml, module)?)?;
    module.add_function(wrap_pyfunction!(document_api::write_xml, module)?)?;
    module.add_function(wrap_pyfunction!(analysis_api::undeclared_variables, module)?)?;
    module.add_function(wrap_pyfunction!(composer::init_programmatic_subdoc, module)?)?;
    module.add_function(wrap_pyfunction!(composer::init_file_subdoc, module)?)?;
    module.add_function(wrap_pyfunction!(composer::programmatic_subdoc_xml, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_constants, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_attach_parts, module)?)?;
    module.add_function(wrap_pyfunction!(composer::reset_composer, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_fix_section_types, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_remove_header_and_footer_references, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_renumber_bookmarks, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_renumber_drawing_ids, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_add_referenced_parts, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_add_relationship, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_add_images, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_add_shapes, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_add_styles_from_other_parts, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_insert_numbering, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_replace_mapped_num_id, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_numbering_part, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_footnote_part, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_add_footnotes, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_next_numbering_ids, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_add_numberings, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_add_styles, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_restart_first_numbering, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_create_style_id_mapping, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_mapped_style_id, module)?)?;
    module.add_function(wrap_pyfunction!(live_routing::requires_live_model, module)?)?;
    module.add_function(wrap_pyfunction!(live_routing::collect_specials, module)?)?;
    module.add_function(wrap_pyfunction!(live_routing::simple_context_names, module)?)?;
    module.add_function(wrap_pyfunction!(inline_image::image_init, module)?)?;
    module.add_function(wrap_pyfunction!(inline_image::image_set_properties, module)?)?;
    module.add_function(wrap_pyfunction!(inline_image::image_add_hyperlink, module)?)?;
    module.add_function(wrap_pyfunction!(inline_image::image_insert, module)?)?;
    module.add_function(wrap_pyfunction!(live_xml::render_live_xml, module)?)?;
    module.add_function(wrap_pyfunction!(live_properties::render_live_properties, module)?)?;
    module.add_function(wrap_pyfunction!(live_footnotes::render_live_footnotes, module)?)?;
    module.add_function(wrap_pyfunction!(live_headers::render_live_headers, module)?)?;
    module.add_function(wrap_pyfunction!(live_body::render_live_body, module)?)?;
    module.add_function(wrap_pyfunction!(live_body::commit_live_body, module)?)?;
    module.add_function(wrap_pyfunction!(buffer_binding::base64_encode, module)?)?;
    module.add_function(wrap_pyfunction!(value_binding::encode_value, module)?)?;
    module.add_function(wrap_pyfunction!(read_binding::read_source, module)?)?;
    module.add_function(wrap_pyfunction!(composer::composer_add_diagrams, module)?)?;
    module.add_function(wrap_pyfunction!(escape_listing, module)?)?;
    module.add_function(wrap_pyfunction!(python_richtext_run, module)?)?;
    module.add_function(wrap_pyfunction!(python_richtext_paragraph, module)?)?;
    module.add_function(wrap_pyfunction!(richtext_binding::init_rich_value, module)?)?;
    module.add_function(wrap_pyfunction!(richtext_binding::add_richtext, module)?)?;
    module.add_function(wrap_pyfunction!(richtext_binding::add_richtext_paragraph, module)?)?;
    module.add_function(wrap_pyfunction!(simple_source_accepts, module)?)?;
    module.add_class::<HyperlinkSource>()?;
    module.add("STACK", "docxtplrs+MiniJinja+rsyi/jinja+libxml2")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn python_bytes_are_borrowed_until_shared_storage_is_created() {
        use pyo3::types::PyBytesMethods;
        Python::initialize();
        Python::attach(|py| {
            let bytes = PyBytes::new(py, b"immutable input");
            let pointer = bytes.as_bytes().as_ptr();
            let captured = capture_source(bytes.as_any()).unwrap();
            match &captured {
                SourceBuffer::Python(source) => assert_eq!(source.as_ref().as_ptr(), pointer),
                SourceBuffer::Owned(_) => panic!("exact bytes should retain their Python buffer"),
            }
            drop(bytes);
            let shared = py.detach(move || captured.into_shared());
            assert_eq!(&*shared, b"immutable input");
        });
    }


    #[test]
    fn richtext_font_follows_style_before_color() {
        let props = docxtplrs::richtext::TextProps {
            style: Some("Strong".into()), font: Some("eastAsia:Arial".into()),
            color: Some("#AABBCC".into()), bold: true, ..Default::default()
        };
        let xml = docxtplrs::richtext::richtext_run("<&", &props);
        assert_eq!(xml, "<w:r><w:rPr><w:rStyle w:val=\"Strong\"/><w:rFonts w:ascii=\"Arial\" w:hAnsi=\"Arial\" w:cs=\"Arial\" w:eastAsia=\"Arial\"/><w:color w:val=\"AABBCC\"/><w:b/></w:rPr><w:t xml:space=\"preserve\">&lt;&amp;</w:t></w:r>");
    }
    #[test]
    fn installs_extended_jinja_filters() {
        let core = TplCore::default();
        let mut env = docxtplrs::template::make_env(false, &core);
        jinja::add_jinja2_compat(&mut env);
        assert_eq!(env.template_from_str("{{ 'x'|center(3) }}").unwrap().render(()).unwrap(), " x ");
    }

    #[test]
    fn context_maps_preserve_insertion_order_and_safe_xml() {
        let mut core = TplCore::default();
        let input = serde_json::json!({"kind":"map","items":[["values",{
            "kind":"map","items":[["z",{"kind":"str","value":"first"}],
                                     ["a",{"kind":"xml","value":"<b/>"}]]
        }]]});
        let context = context_value(&input, &mut core).unwrap();
        let env = docxtplrs::template::make_env(true, &core);
        let output = env.template_from_str("{{ values|list|join(',') }}|{{ values.a }}")
            .unwrap().render(context).unwrap();
        assert_eq!(output, "z,a|<b/>");
    }

    #[test]
    fn core_properties_use_namespace_uris_and_keep_existing_values() {
        let source = r#"<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:other="http://purl.org/dc/elements/1.1/"><other:title>Keep this</other:title></cp:coreProperties>"#;
        let updated = complete_core_properties(source).unwrap().unwrap();
        assert!(updated.contains("<other:title>Keep this</other:title>"));
        assert!(updated.contains("<other:identifier/>"));
        assert!(updated.contains("<other:language/>"));
        assert!(complete_core_properties(&updated).unwrap().is_none());
    }

    #[test]
    fn vm_distinguishes_attribute_and_item_protocols() {
        #[derive(Debug)]
        struct Dual;
        impl minijinja::value::Object for Dual {
            fn get_value(self: &Arc<Self>, _key: &Value) -> Option<Value> { Some(Value::from("item")) }
            fn get_attribute(self: &Arc<Self>, _key: &str) -> Option<Value> { Some(Value::from("attribute")) }
        }
        let context: Value = [("value", Value::from_object(Dual))].into_iter().collect();
        let env = minijinja::Environment::new();
        assert_eq!(env.template_from_str("{{ value.name }}|{{ value['name'] }}").unwrap().render(context).unwrap(), "attribute|item");
    }

    #[test]
    fn vm_stops_before_branch_side_effects_when_truthiness_fails() {
        #[derive(Debug)]
        struct Broken;
        impl minijinja::value::Object for Broken {
            fn is_true_fallible(self: &Arc<Self>) -> Result<bool, minijinja::Error> {
                Err(minijinja::Error::new(minijinja::ErrorKind::InvalidOperation, "truthiness failure"))
            }
        }
        let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let counter = calls.clone();
        let mut env = minijinja::Environment::new();
        env.add_function("after", move || { counter.fetch_add(1, std::sync::atomic::Ordering::Relaxed); "after" });
        let context: Value = [("value", Value::from_object(Broken))].into_iter().collect();
        let result = env.template_from_str("{% if value %}yes{% else %}{{ after() }}{% endif %}").unwrap().render(context);
        assert!(result.unwrap_err().to_string().contains("truthiness failure"));
        assert_eq!(calls.load(std::sync::atomic::Ordering::Relaxed), 0);
    }
}

