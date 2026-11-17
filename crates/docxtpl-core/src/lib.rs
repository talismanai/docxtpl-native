//! Rust DOCX templating core, shared by language bindings.
use base64::Engine as _;
use docxtplrs::template::{Deferred, EnvCache, TplCore};
use minijinja::Value;
use serde_json::Value as Json;
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use unicode_categories::UnicodeCategories;

pub mod opc;
pub mod foreign_xml;
pub mod image_stream;
pub mod unicode_transport;
mod unicode_word;
mod provenance;

/// Scope a document part so a host binding can materialize registered images
/// during callbacks without holding a mutable package pointer.
pub fn with_callback_image_scope(core: &mut TplCore, part: &str,
    render: &dyn Fn(&mut TplCore) -> Result<String, String>, owner: usize) -> Result<String, String> {
    provenance::with_owned_image_render_scope(core, part, render, Some(owner))
}

/// Scope a part for registered-image materialization when no host owner was supplied.
pub fn with_registered_image_scope(core: &mut TplCore, part: &str,
    render: &dyn Fn(&mut TplCore) -> Result<String, String>) -> Result<String, String> {
    provenance::with_image_render_scope(core, part, render)
}

/// Materialize an already registered image in the active part, if present.
pub fn materialize_current_registered_image(identity: usize) -> Option<Result<String, String>> {
    provenance::materialize_current_registered_image(identity)
}

/// Materialize an explicit registered deferred index in the active part.
pub fn materialize_current_deferred(index: usize) -> Option<Result<String, String>> {
    provenance::materialize_current_deferred(index)
}

/// Whether the active part has this identity registered as an image.
pub fn is_current_registered_image(identity: usize) -> bool {
    provenance::is_current_registered_image(identity)
}

/// Test whether an image belongs to the active callback scope.
pub fn is_callback_image(identity: usize, owner: usize) -> bool {
    provenance::is_registered_image(identity, owner) || provenance::is_image_owner(owner)
}

/// Whether Python callbacks are executing inside an owned document part.
pub fn has_owned_callback_image_scope() -> bool {
    provenance::has_owned_image_scope()
}

/// Whether a document part is rendering without a host owner for new images.
pub fn has_unowned_callback_image_scope() -> bool {
    provenance::has_unowned_image_scope()
}

/// Materialize a registered image in the active callback scope if present.
pub fn materialize_callback_image(identity: usize, image: Deferred) -> Option<Result<String, String>> {
    provenance::materialize_registered_image(identity, image)
}

/// Reference Unicode versions for the supported Python runtime targets.
#[derive(Clone, Copy)]
pub enum UnicodeWordVersion { Python311, Python312, Unqualified }

pub fn word_character(character: char, version: UnicodeWordVersion) -> bool {
    if character == '_' { return true; }
    if character.is_ascii() { return character.is_ascii_alphanumeric(); }
    if matches!(version, UnicodeWordVersion::Unqualified) {
        return character.is_alphanumeric() && !character.is_mark();
    }
    fn contains(ranges: &[(u32, u32)], value: u32) -> bool {
        let index = ranges.partition_point(|&(_, end)| end < value);
        ranges.get(index).is_some_and(|&(start, _)| start <= value)
    }
    let value = character as u32;
    contains(unicode_word::WORD_14, value)
        || (matches!(version, UnicodeWordVersion::Python312)
            && contains(unicode_word::WORD_15_ADDITIONS, value))
}

/// Match the reference's XML-declaration encoding lookup for Unicode scalar strings.
pub fn headers_footers_encoding(xml: &str) -> &str {
    headers_footers_encoding_with_unicode(xml, UnicodeWordVersion::Python312)
}

/// Match the regex word boundary using the selected reference Unicode version.
pub fn headers_footers_encoding_with_unicode(xml: &str, version: UnicodeWordVersion) -> &str {
    let Some(prefix) = xml.get(..5) else { return "utf-8" };
    if !prefix.eq_ignore_ascii_case("<?xml") { return "utf-8"; }
    let mut answer = None;
    let body = &xml[5..];
    for pos in memchr::memchr3_iter(b'e', b'E', b'?', body.as_bytes()) {
        if body.as_bytes()[pos] == b'?' { break; }
        if pos == 0 { continue; }
        let previous = body[..pos].chars().next_back().unwrap();
        if word_character(previous, version) { continue; }
        let mut chars = body[pos..].char_indices();
        let mut end = 0;
        let mut matched = true;
        for expected in "encoding".chars() {
            let Some((offset, actual)) = chars.next() else { matched = false; break; };
            let equal = actual.eq_ignore_ascii_case(&expected)
                || (expected == 'i' && matches!(actual, '\u{130}' | '\u{131}'));
            if !equal { matched = false; break; }
            end = offset + actual.len_utf8();
        }
        if !matched { continue; }
        let rest = &body[pos + end..];
        let Some(value) = rest.strip_prefix("=\"") else { continue; };
        let Some(close) = value.find('"') else { continue; };
        if close > 0 { answer = Some(&value[..close]); }
    }
    answer.unwrap_or("utf-8")
}

pub fn xml_check(source: &[u8]) -> Result<(), String> {
    let options = libxml::parser::ParserOptions { recover: false, no_net: true, ..Default::default() };
    libxml::parser::Parser::default().parse_string_with_options(source, options)
        .map(|_| ()).map_err(|_| "libxml2 rejected XML".to_owned())
}

/// docxtpl assigns all six string core properties, even when their initial
/// value is empty. python-docx creates missing elements as a side effect.
pub fn complete_core_properties(source: &str) -> Result<Option<String>, String> {
    const DC: &str = "http://purl.org/dc/elements/1.1/";
    let options = libxml::parser::ParserOptions { recover: false, no_net: true, ..Default::default() };
    let doc = libxml::parser::Parser::default().parse_string_with_options(source, options)
        .map_err(|_| "invalid core properties XML".to_owned())?;
    let mut root = doc.get_root_element().ok_or("missing core properties root")?;
    let namespaces = root.get_namespaces(&doc);
    let namespace = match namespaces.iter().find(|ns| ns.get_href() == DC) {
        Some(ns) => ns.clone(),
        None => {
            let prefix = (0..).map(|n| format!("docxtpldc{n}"))
                .find(|prefix| !namespaces.iter().any(|ns| ns.get_prefix() == *prefix)).unwrap();
            libxml::tree::Namespace::new(&prefix, DC, &mut root).map_err(|e| e.to_string())?
        }
    };
    let mut changed = false;
    for name in ["creator", "description", "identifier", "language", "subject", "title"] {
        let present = root.get_child_elements().iter().any(|child|
            child.get_name() == name && child.get_namespace().is_some_and(|ns| ns.get_href() == DC));
        if !present {
            root.new_child(Some(namespace.clone()), name).map_err(|e| e.to_string())?;
            changed = true;
        }
    }
    Ok(changed.then(|| doc.to_string()))
}

fn decode_bytes(node: &Json) -> Result<Vec<u8>, String> {
    base64::engine::general_purpose::STANDARD
        .decode(node.as_str().ok_or("expected base64 text")?)
        .map_err(|e| e.to_string())
}

pub fn context_value(node: &Json, core: &mut TplCore) -> Result<Value, String> {
    context_value_inner(node, core, false, None)
}

/// Construct native typed context values while retaining deferred provenance.
pub fn context_value_with_provenance(node: &Json, core: &mut TplCore) -> Result<Value, String> {
    context_value_inner(node, core, true, None)
}

fn context_value_inner(node: &Json, core: &mut TplCore, provenance: bool,
    arena: Option<&provenance::Arena>) -> Result<Value, String> {
    let kind = node["kind"].as_str().ok_or("missing typed-value kind")?;
    Ok(match kind {
        "none" => Value::from(()),
        "bool" => Value::from(node["value"].as_bool().ok_or("invalid bool")?),
        "int" => Value::from(node["value"].as_str().ok_or("invalid int")?
            .parse::<i128>().map_err(|_| "integer outside native i128 range")?),
        "float" => Value::from(node["value"].as_f64().ok_or("invalid float")?),
        "str" => Value::from(node["value"].as_str().ok_or("invalid string")?.to_owned()),
        "xml" => Value::from_safe_string(node["value"].as_str().ok_or("invalid rich XML")?.to_owned()),
        "list" => {
            let items = node["items"].as_array().ok_or("invalid list")?;
            Value::from(items.iter().map(|v| context_value_inner(v, core, provenance, arena)).collect::<Result<Vec<_>,_>>()?)
        }
        "map" => {
            let entries = node["items"].as_array().ok_or("invalid map")?;
            let mut result = Vec::with_capacity(entries.len());
            for entry in entries {
                result.push((entry[0].as_str().ok_or("invalid key")?.to_owned(), context_value_inner(&entry[1], core, provenance, arena)?));
            }
            // Keep Value objects (safe XML/deferred values) intact rather than
            // converting them through Serde a second time. MiniJinja's
            // preserve_order map also keeps Python dictionary insertion order.
            result.into_iter().collect::<Value>()
        }
        "image" | "subdoc" => {
            let data: Arc<[u8]> = decode_bytes(&node["data"])?.into();
            let deferred = if kind == "image" {
                Deferred::Image { blob: data, info: None,
                    filename: node["filename"].as_str().map(str::to_owned),
                    width: node["width"].as_i64(), height: node["height"].as_i64(),
                    anchor: node["anchor"].as_str().map(str::to_owned),
                    title: node["title"].as_str().map(str::to_owned),
                    descr: node["descr"].as_str().map(str::to_owned) }
            } else { Deferred::Subdoc { bytes: Some(data), keep_sections: false } };
            let index = core.deferred.len();
            let tagged_index = if let Some(arena) = arena {
                Some(arena.deferred(index).map_err(|error| error.to_string())?)
            } else if provenance {
                Some(u32::try_from(index).map_err(|_| "deferred index exceeds u32")?)
            } else { None };
            core.deferred.push(deferred);
            let token = docxtplrs::template::deferred_token(index);
            match tagged_index {
                Some(index) => Value::from_safe_string_with_provenance(token, index),
                None => Value::from_safe_string(token),
            }
        }
        _ => return Err(format!("unsupported typed-value kind {kind}")),
    })
}

pub fn prepare(source: Vec<u8>, replacements: &Json) -> Result<TplCore, String> {
    prepare_shared(source.into(), replacements)
}

/// Construct a core using an already shared immutable DOCX buffer.
/// The archive buffer is retained, not copied, at this boundary.
pub fn prepare_shared(source: Arc<[u8]>, replacements: &Json) -> Result<TplCore, String> {
    let mut core = TplCore { original_bytes: source, footnote_locator: Some(opc::footnote_parts), ..Default::default() };
    core.init_docx(false)?;
    for (name, target) in [("media", &mut core.crc_to_new_media), ("embedded", &mut core.crc_to_new_embedded)] {
        if let Some(entries) = replacements[name].as_object() {
            for (key, value) in entries { target.insert(key.parse::<u32>().map_err(|e| e.to_string())?, decode_bytes(value)?); }
        }
    }
    for (name, target) in [("zipname", &mut core.zipname_to_replace), ("pictures", &mut core.pics_to_replace)] {
        if let Some(entries) = replacements[name].as_object() {
            for (key, value) in entries { target.insert(key.clone(), decode_bytes(value)?); }
        }
    }
    Ok(core)
}

fn configure_provenance_filters(env: &mut minijinja::Environment<'_>) {
    env.add_filter("safe", |state: &minijinja::State<'_, '_>, value: &Value| -> Result<Value, minijinja::Error> {
        use minijinja::value::ArgType as _;
        let (text, _) = String::from_state_and_value(Some(state), Some(value))?;
        if let Some(provenance) = value.string_provenance() {
            return Ok(if value.is_safe() { value.clone() }
                else { Value::from_safe_string_with_provenance(text, provenance) });
        }
        Ok(minijinja::filters::safe(text))
    });
}

fn configure_provenance_runtime(env: &mut minijinja::Environment<'_>, arena: Arc<provenance::Arena>) {
    configure_provenance_filters(env);
    let safe_arena = arena.clone();
    env.add_filter("safe", move |state: &minijinja::State<'_, '_>, value: &Value| -> Result<Value, minijinja::Error> {
        use minijinja::value::ArgType as _;
        if let Some(value) = safe_arena.resolve_images(value)? {
            return Ok(Value::from_safe_string(value.to_string()));
        }
        let (text, _) = String::from_state_and_value(Some(state), Some(value))?;
        if let Some(id) = value.string_provenance() {
            return Ok(if value.is_safe() { value.clone() } else { Value::from_safe_string_with_provenance(text, id) });
        }
        Ok(minijinja::filters::safe(text))
    });
    let string_arena = arena.clone();
    env.add_filter("string", move |state: &minijinja::State<'_, '_>, value: &Value| -> Result<Value, minijinja::Error> {
        if let Some(value) = string_arena.resolve_images(value)? { return Ok(Value::from(value.to_string())); }
        minijinja::filters::string(state, value)
    });
    let join = arena.clone();
    env.add_filter("join", move |state: &minijinja::State<'_, '_>, value: &Value,
        separator: Option<&Value>| -> Result<Value, minijinja::Error> {
        join.join(state, value, separator)
    });
    let concat = arena.clone();
    env.set_runtime_concat(move |_state, values| {
        let values = values.iter().map(|value| Ok(concat.resolve_images(value)?.unwrap_or_else(|| value.clone())))
            .collect::<Result<Vec<_>, minijinja::Error>>()?;
        concat.concatenate(&values)
    });
    let captures = arena.clone();
    env.set_fragment_joiner(move |values, autoescape| captures.capture(values, autoescape));
    env.set_formatter(move |output, state, value| {
        state.undefined_behavior().assert_value_not_undefined(value)?;
        if let Some(resolved) = arena.resolve_images(value)? {
            return minijinja::escape_formatter(output, state, &resolved);
        }
        if let Some(id) = value.string_provenance().filter(|_| output.supports_fragments()) {
            let value = match state.auto_escape() {
                minijinja::AutoEscape::None => value.clone(),
                minijinja::AutoEscape::Html if value.is_safe() => value.clone(),
                minijinja::AutoEscape::Html => arena.escape_html(id)?,
                _ => return Err(minijinja::Error::new(minijinja::ErrorKind::InvalidOperation,
                    "fragment output requires a supported autoescape mode")),
            };
            return output.write_fragment(value);
        }
        minijinja::escape_formatter(output, state, value)
    });
}

fn render_native(core: &mut TplCore, autoescape: bool, arena: Option<Arc<provenance::Arena>>,
    make_ctx: &docxtplrs::template::CtxFn<'_>) -> Result<(), String> {
    let mut env = docxtplrs::template::make_env(autoescape, core);
    jinja::add_jinja2_compat(&mut env);
    if let Some(arena) = arena {
        core.fragment_joining = true;
        core.host_fragment_recovery = Some(opc::recover_rendered_xml);
        core.host_part_scope = Some(docxtplrs::template::HostPartScope(Arc::new(
            provenance::with_image_render_scope)));
        let output_arena = arena.clone();
        core.host_fragment_converter = Some(docxtplrs::template::HostFragmentConverter(Arc::new(move |values| {
            let mut output = Vec::new();
            for value in values {
                if let Some(id) = value.string_provenance() {
                    for fragment in output_arena.fragments(id)? {
                        output.push(match fragment {
                            provenance::Fragment::Text(text) => docxtplrs::template::OwnedMaterializationFragment::Text(text),
                            provenance::Fragment::Deferred(index) => docxtplrs::template::OwnedMaterializationFragment::Deferred(index),
                        });
                    }
                } else {
                    output.push(docxtplrs::template::OwnedMaterializationFragment::Text(value.to_string()));
                }
            }
            Ok(output)
        })));
        configure_provenance_runtime(&mut env, arena);
    }
    core.env_caches[usize::from(autoescape)] = Some(EnvCache {
        autoescape, r#gen: core.env_gen, env_options: core.env_options.clone(),
        env, tpl_hashes: HashMap::new(),
    });
    core.render(autoescape, make_ctx)?;
    if let Some(source) = core.package.as_ref().and_then(|pkg| pkg.get_string("docProps/core.xml")) {
        if let Some(updated) = complete_core_properties(&source)? {
            core.package.as_mut().unwrap().set("docProps/core.xml", updated.into_bytes());
        }
    }
    xml_check(core.get_xml()?.as_bytes())?;
    Ok(())
}

/// Load renderable XML parts with python-docx's blank-node policy before
/// rendering, so newly inserted renderer text nodes are not stripped on save.
fn normalize_render_parts(core: &mut TplCore) -> Result<HashSet<String>, String> {
    use docxtplrs::package::{rel_type, resolve_target};
    use docxtplrs::template::DOCUMENT_PART;
    let package = core.package.as_ref().ok_or("package not loaded")?;
    let mut parts = vec![DOCUMENT_PART.to_owned()];
    let relationships = package.rels(DOCUMENT_PART);
    for kind in [rel_type::HEADER, rel_type::FOOTER] {
        parts.extend(relationships.by_type(kind).filter(|rel| !rel.is_external)
            .map(|rel| resolve_target(DOCUMENT_PART, &rel.target)));
    }
    let mut normalized_parts = HashSet::new();
    for part in parts {
        if normalized_parts.contains(&part) { continue; }
        let package = core.package.as_mut().ok_or("package not loaded")?;
        let Some(source) = package.get_string(&part) else { continue; };
        let normalized = opc::normalize_xml_part(&source)?;
        package.set(&part, format!("<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n{normalized}").into_bytes());
        core.invalidate_part(&part);
        normalized_parts.insert(part);
    }
    // Headers and footers are reparsed with blank-node removal when mapped;
    // only the main document retains text nodes introduced during rendering.
    normalized_parts.retain(|part| part == DOCUMENT_PART);
    Ok(normalized_parts)
}

/// Existing typed-JSON adapter, retained for the Python binding.
pub fn process(source: Vec<u8>, payload: &str, autoescape: bool, render: bool) -> Result<Vec<u8>, String> {
    process_shared(source.into(), payload, autoescape, render)
}

/// Process a shared immutable input without copying its archive at this boundary.
pub fn process_shared(source: Arc<[u8]>, payload: &str, autoescape: bool, render: bool) -> Result<Vec<u8>, String> {
    process_shared_inner(source, payload, autoescape, render, false, false, None)
}

/// Process typed input with a host-owned integer protocol for numbering ids.
/// `source_rendered` marks unrendered processing of bytes a render produced,
/// whose core properties were already assigned.
pub fn process_shared_with_numbering_protocols(source: Arc<[u8]>, payload: &str, autoescape: bool, render: bool,
    source_rendered: bool, integer: docxtplrs::template::NumberingInteger) -> Result<Vec<u8>, String> {
    process_shared_inner(source, payload, autoescape, render, false, source_rendered, Some(integer))
}

/// Repackage bytes just serialized by a caller-owned document model without
/// reparsing its XML parts and losing explicit empty text nodes.
pub fn process_model_shared(source: Arc<[u8]>, payload: &str) -> Result<Vec<u8>, String> {
    process_shared_inner(source, payload, false, false, true, false, None)
}

fn process_shared_inner(source: Arc<[u8]>, payload: &str, autoescape: bool, render: bool, preserve_model: bool,
    source_rendered: bool, integer: Option<docxtplrs::template::NumberingInteger>) -> Result<Vec<u8>, String> {
    let payload: Json = serde_json::from_str(payload).map_err(|e| e.to_string())?;
    let mut core = prepare_shared(source, &payload["replacements"])?;
    core.numbering_integer = integer;
    let mut rendered_parts = None;
    if render {
        core.python_image_contract = true;
        core.python_subdoc_numbering_contract = true;
        // XmlPart.load parses each rendered header/footer immediately, before
        // later parts and properties are evaluated in the reference pipeline.
        core.header_footer_normalizer = Some(opc::normalize_rendered_header_footer);
        rendered_parts = Some(normalize_render_parts(&mut core)?);
        let arena = Arc::new(provenance::Arena::default());
        render_native(&mut core, autoescape, Some(arena.clone()), &|core, _part|
            context_value_inner(&payload["context"], core, true, Some(&arena)))?;
    }
    // Without a completed render, python-docx still parses and reserializes
    // the loaded core properties without assigning them.
    let core_policy = if render || preserve_model || source_rendered { CorePolicy::Rendered } else { CorePolicy::Loaded };
    save_compatible_inner(&mut core, preserve_model, rendered_parts.as_ref(), core_policy)
}

/// Render a basic serde-compatible Rust context. This API is provisional.
pub fn render<T: serde::Serialize>(source: Vec<u8>, context: T, autoescape: bool) -> Result<Vec<u8>, String> {
    render_shared(source.into(), context, autoescape)
}

/// As `render`, retaining an existing immutable archive allocation.
pub fn render_shared<T: serde::Serialize>(source: Arc<[u8]>, context: T, autoescape: bool) -> Result<Vec<u8>, String> {
    let mut core = prepare_shared(source, &Json::Null)?;
    let context = Value::from_serialize(context);
    render_native(&mut core, autoescape, None, &|_core, _part| Ok(context.clone()))?;
    save_compatible(&mut core)
}

pub fn save_compatible(core: &mut TplCore) -> Result<Vec<u8>, String> {
    save_compatible_inner(core, false, None, CorePolicy::Rendered)
}

/// Whether the reference assigned core properties before saving.
#[derive(Clone, Copy)]
enum CorePolicy { Rendered, Loaded }

/// Preserve text nodes introduced in the rendered body, as python-docx does
/// after replacing its body tree. Header/footer parts are normalized on load.
pub fn save_rendered_compatible(core: &mut TplCore) -> Result<Vec<u8>, String> {
    let rendered = HashSet::from([docxtplrs::template::DOCUMENT_PART.to_owned()]);
    save_compatible_inner_raw_body(core, false, Some(&rendered), false, CorePolicy::Rendered)
}

fn save_compatible_inner(core: &mut TplCore, preserve_model: bool, rendered_parts: Option<&HashSet<String>>,
    core_policy: CorePolicy) -> Result<Vec<u8>, String> {
    save_compatible_inner_raw_body(core, preserve_model, rendered_parts, false, core_policy)
}

/// Save a body serialized by the host XML recovery provider without reparsing it.
pub fn save_recovered_body_compatible(core: &mut TplCore) -> Result<Vec<u8>, String> {
    let rendered = HashSet::from([docxtplrs::template::DOCUMENT_PART.to_owned()]);
    save_compatible_inner_raw_body(core, false, Some(&rendered), true, CorePolicy::Rendered)
}

fn save_compatible_inner_raw_body(core: &mut TplCore, preserve_model: bool, rendered_parts: Option<&HashSet<String>>,
    preserve_raw_body: bool, core_policy: CorePolicy) -> Result<Vec<u8>, String> {
    core.flush_parts()?;
    core.init_docx(false)?;
    core.pre_processing()?;
    let package = core.package.as_ref().ok_or("package not loaded")?;
    let bytes = if preserve_model { opc::serialize_model(package)? }
        else if let Some(parts) = rendered_parts {
            if preserve_raw_body { opc::serialize_rendered_with_raw_body(package, parts)? }
            else { opc::serialize_rendered(package, parts)? }
        }
        else if let CorePolicy::Loaded = core_policy { opc::serialize_loaded(package)? }
        else { opc::serialize(package)? };
    // Raw ZIP replacements occur after OPC writing in the reference pipeline.
    let bytes = if core.crc_to_new_media.is_empty() && core.crc_to_new_embedded.is_empty()
        && core.zipname_to_replace.is_empty() { bytes } else { core.post_processing_bytes(&bytes)? };
    core.is_saved = true;
    Ok(bytes)
}

#[cfg(test)]
mod provenance_tests {
    use super::{configure_provenance_filters, configure_provenance_runtime, context_value, context_value_with_provenance, provenance};
    use docxtplrs::template::{deferred_token, TplCore};
    use minijinja::Value;
    use std::sync::Arc;

    #[test]
    fn typed_context_retains_provenance_at_all_nested_paths() {
        let token = deferred_token(0);
        let input = serde_json::json!({"kind": "map", "items": [
            ["text", {"kind": "str", "value": token}],
            ["items", {"kind": "list", "items": [{"kind": "image", "data": "AA=="}]}],
        ]});
        let mut core = TplCore::default();
        let context = context_value_with_provenance(&input, &mut core).unwrap();
        let text = context.get_item(&Value::from("text")).unwrap();
        let image = context.get_item(&Value::from("items")).unwrap().get_item(&Value::from(0)).unwrap();
        assert_eq!(text.as_str(), image.as_str());
        assert_eq!(text.string_provenance(), None);
        assert_eq!(image.string_provenance(), Some(0));
        assert!(image.is_safe());

        let mut legacy = TplCore::default();
        let context = context_value(&input, &mut legacy).unwrap();
        let image = context.get_item(&Value::from("items")).unwrap().get_item(&Value::from(0)).unwrap();
        assert_eq!(image.string_provenance(), None);
        assert!(image.is_safe());
    }

    #[test]
    fn native_safe_filter_retains_provenance_and_standard_errors() {
        let mut baseline = minijinja::Environment::new();
        let mut typed = minijinja::Environment::new();
        baseline.set_undefined_behavior(minijinja::UndefinedBehavior::Strict);
        typed.set_undefined_behavior(minijinja::UndefinedBehavior::Strict);
        configure_provenance_filters(&mut typed);
        for source in ["{{ value|safe }}", "{{ missing|safe }}", "{{ value|safe(1) }}", "{{ value|safe(unknown=1) }}"] {
            let context = minijinja::context!(value => "<tag>");
            let expected = baseline.template_from_str(source).unwrap().render(&context)
                .map_err(|error| (error.kind(), error.detail().map(str::to_owned)));
            let actual = typed.template_from_str(source).unwrap().render(&context)
                .map_err(|error| (error.kind(), error.detail().map(str::to_owned)));
            assert_eq!(actual, expected, "{source}");
        }
        typed.set_formatter(|output, state, value| {
            if value.string_provenance().is_some() { output.write_fragment(value.clone()) }
            else { minijinja::escape_formatter(output, state, value) }
        });
        let joiner = |values: Vec<Value>, _: minijinja::AutoEscape| {
            Ok(Value::from(values.iter().map(ToString::to_string).collect::<String>()))
        };
        let template = typed.template_from_str("{{ image|safe }}").unwrap();
        let value = Value::from_safe_string_with_provenance("<image>".into(), 23);
        let output = template.render_unjoined_fragments(minijinja::context!(image => value), &joiner).unwrap();
        assert_eq!(output.len(), 1);
        assert_eq!(output[0].string_provenance(), Some(23));
        assert_eq!(output[0].as_str(), Some("<image>"));
    }

    #[test]
    fn native_concat_preserves_nested_origins_and_autoescape() {
        let arena = Arc::new(provenance::Arena::default());
        let image_id = arena.deferred(9).unwrap();
        let image = Value::from_safe_string_with_provenance(deferred_token(9), image_id);
        let fake = deferred_token(9);
        let mut environment = minijinja::Environment::new();
        let concat = arena.clone();
        environment.set_runtime_concat(move |_state, values| concat.concatenate(values));
        configure_provenance_filters(&mut environment);
        environment.set_auto_escape_callback(|_| minijinja::AutoEscape::Html);
        environment.set_formatter(|output, state, value| {
            if value.string_provenance().is_some() { output.write_fragment(value.clone()) }
            else { minijinja::escape_formatter(output, state, value) }
        });
        let joiner = |values: Vec<Value>, _: minijinja::AutoEscape| {
            Ok(Value::from(values.iter().map(ToString::to_string).collect::<String>()))
        };
        let template = environment.template_from_str("{{ (fake ~ image ~ tail)|safe }}").unwrap();
        let output = template.render_unjoined_fragments(minijinja::context!(
            fake => fake.clone(), image => image.clone(), tail => "<end>"
        ), &joiner).unwrap();
        assert_eq!(output.len(), 1);
        assert!(output[0].is_safe());
        let id = output[0].string_provenance().unwrap();
        assert_eq!(arena.fragments(id).unwrap(), vec![
            provenance::Fragment::Text(fake.clone()), provenance::Fragment::Deferred(9),
            provenance::Fragment::Text("<end>".to_owned()),
        ]);
        assert_eq!(output[0].as_str(), Some(format!("{fake}{}<end>", deferred_token(9)).as_str()));

        let mut baseline = minijinja::Environment::new();
        baseline.set_auto_escape_callback(|_| minijinja::AutoEscape::Html);
        let mut typed = minijinja::Environment::new();
        typed.set_auto_escape_callback(|_| minijinja::AutoEscape::Html);
        let concat = arena.clone();
        typed.set_runtime_concat(move |_state, values| concat.concatenate(values));
        for source in ["{{ a ~ b }}", "{{ a ~ b ~ c }}", "{{ a ~ image }}"] {
            let context = minijinja::context!(a => "<a>", b => 3, c => false, image => image.clone());
            assert_eq!(typed.template_from_str(source).unwrap().render(&context).unwrap(),
                baseline.template_from_str(source).unwrap().render(&context).unwrap(), "{source}");
        }
    }

    #[test]
    fn native_captures_and_concat_keep_text_and_deferred_values_separate() {
        let fake = deferred_token(0);
        for autoescape in [minijinja::AutoEscape::None, minijinja::AutoEscape::Html] {
            for source in ["{{ fake }}{{ image }}", "{% set x %}{{ fake }}{{ image }}{% endset %}{{ x }}",
                "{% macro item() %}{{ fake }}{{ image }}{% endmacro %}{{ item() }}",
                "{{ fake ~ image }}", "{{ image|string }}{{ fake }}", "{{ image|safe }}{{ fake }}"] {
                let arena = Arc::new(provenance::Arena::default());
                let image_id = arena.deferred(0).unwrap();
                let image = Value::from_safe_string_with_provenance(fake.clone(), image_id);
                let mut environment = minijinja::Environment::new();
                environment.set_auto_escape_callback(move |_| autoescape);
                configure_provenance_runtime(&mut environment, arena.clone());
                let template = environment.template_from_str(source).unwrap();
                let context = minijinja::context!(fake => fake.clone(), image => image);
                let output = template.render_unjoined_fragments(&context, environment.fragment_joiner().unwrap()).unwrap();
                let mut pieces = Vec::new();
                for value in output {
                    match value.string_provenance() {
                        Some(id) => pieces.extend(arena.fragments(id).unwrap()),
                        None => pieces.push(provenance::Fragment::Text(value.to_string())),
                    }
                }
                assert_eq!(pieces.iter().filter(|piece| matches!(piece, provenance::Fragment::Deferred(0))).count(), 1, "{source}");
                assert_eq!(pieces.iter().filter_map(|piece| match piece {
                    provenance::Fragment::Text(text) => Some(text.as_str()), _ => None,
                }).collect::<String>(), fake, "{source}");
            }
        }
    }

    #[test]
    fn native_join_retains_deferred_items_and_separators() {
        for autoescape in [minijinja::AutoEscape::None, minijinja::AutoEscape::Html] {
            for (source, expected_count) in [
                ("{{ [fake, image, '<tail>']|join(',') }}", 1),
                ("{% set x %}{{ [fake, image]|join('<&') }}{% endset %}{{ x }}", 1),
                ("{{ [fake, 'a', 'b']|join(image) }}", 2),
            ] {
                let arena = Arc::new(provenance::Arena::default());
                let fake = deferred_token(0);
                let image = Value::from_safe_string_with_provenance(fake.clone(), arena.deferred(0).unwrap());
                let context = minijinja::context!(image => image, fake => fake.clone());
                let mut baseline = minijinja::Environment::new();
                baseline.set_auto_escape_callback(move |_| autoescape);
                let expected = baseline.template_from_str(source).unwrap().render(&context).unwrap();
                let mut typed = minijinja::Environment::new();
                typed.set_auto_escape_callback(move |_| autoescape);
                configure_provenance_runtime(&mut typed, arena.clone());
                let output = typed.template_from_str(source).unwrap().render_unjoined_fragments(
                    &context, typed.fragment_joiner().unwrap()).unwrap();
                assert_eq!(output.iter().map(ToString::to_string).collect::<String>(), expected, "{source}, {autoescape:?}");
                let mut fragments = Vec::new();
                for value in output {
                    if let Some(id) = value.string_provenance() { fragments.extend(arena.fragments(id).unwrap()); }
                    else { fragments.push(provenance::Fragment::Text(value.to_string())); }
                }
                assert_eq!(fragments.iter().filter(|fragment| matches!(fragment,
                    provenance::Fragment::Deferred(0))).count(), expected_count, "{source}");
                assert!(fragments.iter().any(|fragment| matches!(fragment,
                    provenance::Fragment::Text(text) if text.contains(&fake))), "{source}");
            }
        }
    }

    #[test]
    fn native_join_keeps_ordinary_results_and_errors() {
        for autoescape in [minijinja::AutoEscape::None, minijinja::AutoEscape::Html] {
            for source in ["{{ items|join }}", "{{ items|join('<&') }}", "{{ items|join(none) }}",
                "{{ items|join(2) }}", "{{ missing|join }}", "{{ 42|join }}", "{{ items|join(',', 1) }}"] {
                let mut baseline = minijinja::Environment::new();
                baseline.set_auto_escape_callback(move |_| autoescape);
                let context = minijinja::context!(items => vec!["<a>", "b"]);
                let expected = baseline.template_from_str(source).unwrap().render(&context)
                    .map_err(|error| (error.kind(), error.detail().map(str::to_owned)));
                let mut typed = minijinja::Environment::new();
                typed.set_auto_escape_callback(move |_| autoescape);
                configure_provenance_runtime(&mut typed, Arc::new(provenance::Arena::default()));
                let actual = typed.template_from_str(source).unwrap().render(&context)
                    .map_err(|error| (error.kind(), error.detail().map(str::to_owned)));
                assert_eq!(actual, expected, "{source}, {autoescape:?}");
            }
        }
    }

    #[test]
    fn native_join_preserves_lazy_conversion_order() {
        use minijinja::value::{Enumerator, Object, ObjectRepr};
        use std::sync::Mutex;
        #[derive(Debug)]
        struct Item { index: usize, events: Arc<Mutex<Vec<String>>> }
        impl Object for Item {
            fn render(self: &Arc<Self>, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                self.events.lock().unwrap().push(format!("render{}", self.index));
                write!(f, "{}", self.index)
            }
        }
        #[derive(Debug)]
        struct Items { events: Arc<Mutex<Vec<String>>> }
        impl Object for Items {
            fn repr(self: &Arc<Self>) -> ObjectRepr { ObjectRepr::Iterable }
            fn enumerate(self: &Arc<Self>) -> Enumerator {
                let events = self.events.clone();
                Enumerator::Iter(Box::new((0..3).map(move |index| {
                    events.lock().unwrap().push(format!("next{index}"));
                    Value::from_object(Item { index, events: events.clone() })
                })))
            }
        }
        for autoescape in [minijinja::AutoEscape::None, minijinja::AutoEscape::Html] {
            for separator in [Value::from(","), Value::from_safe_string("&".to_owned())] {
                let run = |typed: bool| {
                    let events = Arc::new(Mutex::new(Vec::new()));
                    let context = minijinja::context!(items => Value::from_object(Items { events: events.clone() }),
                        separator => separator.clone());
                    let mut env = minijinja::Environment::new();
                    env.set_auto_escape_callback(move |_| autoescape);
                    if typed { configure_provenance_runtime(&mut env, Arc::new(provenance::Arena::default())); }
                    let result = env.template_from_str("{{ items|join(separator) }}").unwrap().render(&context).unwrap();
                    let events = events.lock().unwrap().clone();
                    (result, events)
                };
                assert_eq!(run(true), run(false), "{autoescape:?}");
            }
        }
    }

    #[test]
    fn native_fragment_runtime_retains_previous_autoescape_behavior() {
        let sources = ["{{ text }}{{ image }}", "{{ text ~ image }}",
            "{% set x %}{{ text }}{{ image }}{% endset %}{{ x }}",
            "{% macro m() %}{{ text }}{{ image }}{% endmacro %}{{ m() }}",
            "{{ (text ~ image)|safe }}", "{{ image|string }}{{ text }}"];
        for autoescape in [minijinja::AutoEscape::None, minijinja::AutoEscape::Html] {
            for source in sources {
                let arena = Arc::new(provenance::Arena::default());
                let image = Value::from_safe_string_with_provenance(deferred_token(0), arena.deferred(0).unwrap());
                let context = minijinja::context!(text => "<a&b/'\">", image => image);
                let mut baseline = minijinja::Environment::new();
                baseline.set_auto_escape_callback(move |_| autoescape);
                let expected = baseline.template_from_str(source).unwrap().render(&context).unwrap();
                let mut typed = minijinja::Environment::new();
                typed.set_auto_escape_callback(move |_| autoescape);
                configure_provenance_runtime(&mut typed, arena);
                let actual = typed.template_from_str(source).unwrap()
                    .render_fragments(&context, typed.fragment_joiner().unwrap()).unwrap().to_string();
                assert_eq!(actual, expected, "{source}, {autoescape:?}");
            }
        }
    }

    #[test]
    fn native_fragment_runtime_retains_undefined_output_behavior() {
        for behavior in [minijinja::UndefinedBehavior::Lenient, minijinja::UndefinedBehavior::SemiStrict,
            minijinja::UndefinedBehavior::Strict] {
            for source in ["{{ missing }}", "{{ missing|default('fallback') }}"] {
                let mut baseline = minijinja::Environment::new();
                baseline.set_undefined_behavior(behavior);
                let expected = baseline.template_from_str(source).unwrap().render(())
                    .map_err(|error| (error.kind(), error.detail().map(str::to_owned)));
                let mut typed = minijinja::Environment::new();
                typed.set_undefined_behavior(behavior);
                configure_provenance_runtime(&mut typed, Arc::new(provenance::Arena::default()));
                let actual = typed.template_from_str(source).unwrap()
                    .render_fragments((), typed.fragment_joiner().unwrap())
                    .map(|value| value.to_string())
                    .map_err(|error| (error.kind(), error.detail().map(str::to_owned)));
                assert_eq!(actual, expected, "{source}, {behavior:?}");
            }
        }
    }
}


