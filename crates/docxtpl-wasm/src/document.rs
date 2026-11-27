//! Render a DOCX with a prepared context (`value::Ctx`): pictures and
//! subdocuments become docxtpl deferred objects, filters can create pictures
//! while a part renders, `string` turns a picture into its run XML as
//! `str(InlineImage)` does, and options select python-docx numbering
//! compatibility.

use std::cell::RefCell;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use docxtplrs::template::{Deferred, EnvCache, HostPartScope, TplCore, deferred_token};
use minijinja::value::{Enumerator, Object, ObjectRepr, Value};

use crate::value::{Ctx, Image};

/// How a document renders.
#[derive(Debug, Clone, Default)]
pub struct DocumentOptions {
    pub autoescape: bool,
    /// Number pictures and composed subdocuments as docxtpl `InlineImage` and
    /// docxcompose do under Python.
    pub python_numbering: bool,
    /// Expose the whole context under this name as well (for example a
    /// host that sets `context["_context"] = context`).
    pub self_reference: Option<String>,
}

/// Pictures of one render. Filters look up the picture a value refers to and
/// create new pictures; those are added to the document after the part that
/// created them renders, in the order they were created.
#[derive(Default, Clone)]
pub struct Pictures(Arc<Mutex<PicturesState>>);

#[derive(Default)]
struct PicturesState {
    images: HashMap<usize, Image>,
    base: usize,
    pending: Vec<Image>,
}

impl Pictures {
    /// The picture a rendered value (an image placeholder) refers to.
    pub fn get(&self, value: &Value) -> Option<Image> {
        self.0.lock().ok()?.images.get(&token_index(value)?).cloned()
    }

    /// A placeholder value for a new picture.
    pub fn create(&self, image: Image) -> Value {
        let mut state = self.0.lock().expect("pictures");
        let index = state.base + state.pending.len();
        state.pending.push(image.clone());
        state.images.insert(index, image);
        Value::from_safe_string(deferred_token(index))
    }

    fn image(&self, index: usize) -> Option<Image> {
        self.0.lock().ok()?.images.get(&index).cloned()
    }
}

fn deferred_image(image: &Image) -> Deferred {
    Deferred::Image { blob: image.bytes.clone(), info: None, filename: None, width: image.width, height: image.height, anchor: None, title: None, descr: None }
}

const TOKEN_START: &str = "\u{1}DTPLD";
const TOKEN_END: char = '\u{1}';

fn token_index(value: &Value) -> Option<usize> {
    if !value.is_safe() { return None; }
    let text = value.as_str()?;
    text.strip_prefix(TOKEN_START)?.strip_suffix(TOKEN_END)?.parse().ok()
}

/// What turning a picture into its XML changes in the package, moved out of
/// the core while a part's template runs (the template does not read it).
struct PartPictures {
    core: TplCore,
    part: String,
}

thread_local! {
    static PART_PICTURES: RefCell<Option<PartPictures>> = const { RefCell::new(None) };
}

/// Run a part's template with its package available to [`materialize_pictures`].
fn with_part_pictures(core: &mut TplCore, part: &str,
    execute: &dyn Fn(&mut TplCore) -> Result<String, String>) -> Result<String, String> {
    let staged = TplCore {
        package: core.package.take(),
        next_shape_ids: std::mem::take(&mut core.next_shape_ids),
        python_image_contract: core.python_image_contract,
        ..TplCore::default()
    };
    let previous = PART_PICTURES.replace(Some(PartPictures { core: staged, part: part.to_owned() }));
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| execute(core)));
    let staged = PART_PICTURES.replace(previous).expect("part pictures").core;
    core.package = staged.package;
    core.next_shape_ids = staged.next_shape_ids;
    match result { Ok(result) => result, Err(panic) => std::panic::resume_unwind(panic) }
}

/// `text` with each picture placeholder replaced by the picture's run XML,
/// the picture added to the part being rendered as `str(InlineImage)` adds
/// it. `None` when `text` holds no picture or no part is rendering; other
/// placeholders (subdocuments) stay for the end of the part.
fn materialize_pictures(text: &str, pictures: &Pictures) -> Result<Option<String>, String> {
    if !text.contains(TOKEN_START) { return Ok(None); }
    PART_PICTURES.with_borrow_mut(|scope| {
        let Some(scope) = scope else { return Ok(None) };
        let mut output = String::with_capacity(text.len());
        let mut rest = text;
        let mut changed = false;
        while let Some(start) = rest.find(TOKEN_START) {
            let after = &rest[start + TOKEN_START.len()..];
            let Some(end) = after.find(TOKEN_END) else { break };
            output.push_str(&rest[..start]);
            match after[..end].parse().ok().and_then(|index| pictures.image(index)) {
                Some(image) => {
                    scope.core.deferred = vec![deferred_image(&image)];
                    output.push_str(&scope.core.materialize_one(&scope.part, 0)?);
                    changed = true;
                }
                None => output.push_str(&rest[start..start + TOKEN_START.len() + end + TOKEN_END.len_utf8()]),
            }
            rest = &after[end + TOKEN_END.len_utf8()..];
        }
        output.push_str(rest);
        Ok(changed.then_some(output))
    })
}

/// `string` as Python's `str()`: a picture is its run XML, as docxtpl's
/// `InlineImage` gives it, so a template can test or edit it
/// (`'pic:pic' in picture|string`, `|replace`). Markup holding pictures stays
/// markup; any other value stringifies as before.
fn add_string_filter(env: &mut minijinja::Environment<'static>, pictures: &Pictures) {
    let pictures = pictures.clone();
    env.add_filter("string", move |state: &minijinja::State<'_, '_>, value: &Value| -> Result<Value, minijinja::Error> {
        if let Some(text) = value.as_str().filter(|_| value.is_safe()) {
            let materialized = materialize_pictures(text, &pictures)
                .map_err(|error| minijinja::Error::new(minijinja::ErrorKind::InvalidOperation, error))?;
            if let Some(xml) = materialized {
                return Ok(if token_index(value).is_some() { Value::from(xml) } else { Value::from_safe_string(xml) });
            }
        }
        minijinja::filters::string(state, value)
    });
}

fn to_value(value: &Ctx, core: &mut TplCore, pictures: &Pictures, subdocument: bool) -> Value {
    match value {
        Ctx::Null => Value::from(()),
        Ctx::Bool(b) => Value::from(*b),
        Ctx::Number(n) => if let Some(i) = n.as_i64() { Value::from(i) } else if let Some(u) = n.as_u64() { Value::from(u) } else { Value::from(n.as_f64().unwrap_or(0.0)) },
        Ctx::Str(s) => Value::from(s.as_str()),
        Ctx::Rich(xml) => Value::from_safe_string(xml.clone()),
        // docxtpl fails when a subdocument renders a picture of the main template.
        Ctx::Image(_) if subdocument => Value::UNDEFINED,
        Ctx::Image(image) => {
            let index = core.deferred.len();
            core.deferred.push(deferred_image(image));
            pictures.0.lock().expect("pictures").images.insert(index, image.clone());
            Value::from_safe_string(deferred_token(index))
        }
        Ctx::Subdoc(bytes) => {
            let index = core.deferred.len();
            core.deferred.push(Deferred::Subdoc { bytes: Some(bytes.clone()), keep_sections: false });
            Value::from_safe_string(deferred_token(index))
        }
        Ctx::List(items) => Value::from(items.iter().map(|i| to_value(i, core, pictures, subdocument)).collect::<Vec<_>>()),
        Ctx::Map(entries) => entries.iter().map(|(k, v)| (k.clone(), to_value(v, core, pictures, subdocument))).collect::<Value>(),
    }
}

/// The top-level context, optionally exposing itself under a name.
#[derive(Debug)]
struct Root {
    entries: Vec<(String, Value)>,
    self_reference: Option<String>,
}

impl Object for Root {
    fn repr(self: &Arc<Self>) -> ObjectRepr { ObjectRepr::Map }
    fn get_value(self: &Arc<Self>, key: &Value) -> Option<Value> {
        let key = key.as_str()?;
        if self.self_reference.as_deref() == Some(key) { return Some(Value::from_dyn_object(self.clone())); }
        self.entries.iter().find(|(k, _)| k == key).map(|(_, v)| v.clone())
    }
    fn enumerate(self: &Arc<Self>) -> Enumerator {
        let mut keys: Vec<Value> = self.entries.iter().map(|(k, _)| Value::from(k.as_str())).collect();
        if let Some(name) = &self.self_reference { keys.push(Value::from(name.as_str())); }
        Enumerator::Values(keys)
    }
}

/// Per-part scope: pictures created by filters during the part are appended
/// to the core after it renders, in the order their placeholders were issued,
/// and the part's package is where `string` adds the pictures it turns into XML.
fn picture_scope(pictures: Option<Pictures>) -> HostPartScope {
    HostPartScope(Arc::new(move |core, part, execute| {
        if let Some(pictures) = &pictures {
            let mut state = pictures.0.lock().expect("pictures");
            state.base = core.deferred.len();
            state.pending.clear();
        }
        let result = if pictures.is_some() { with_part_pictures(core, part, execute) } else { execute(core) };
        if let Some(pictures) = &pictures {
            let pending = std::mem::take(&mut pictures.0.lock().expect("pictures").pending);
            for image in &pending { core.deferred.push(deferred_image(image)); }
        }
        result
    }))
}

fn core_for(template: &[u8], options: &DocumentOptions) -> TplCore {
    // Footnotes always render (docxtpl 0.18+). docxtpl 0.17 left their tags
    // unrendered; that behavior is intentionally not offered.
    let mut core = TplCore::new(template.to_vec());
    core.python_image_contract = options.python_numbering;
    core.python_subdoc_numbering_contract = options.python_numbering;
    core
}

fn install(core: &mut TplCore, env: minijinja::Environment<'static>, autoescape: bool) {
    core.env_caches[usize::from(autoescape)] = Some(EnvCache {
        autoescape, r#gen: core.env_gen, env_options: core.env_options.clone(), env, tpl_hashes: HashMap::new(),
    });
}

/// Render `template` with `context` (a mapping). `configure` adds filters or
/// globals to the environment; filters use `Pictures` to read and create
/// pictures.
pub fn render_document(
    template: &[u8],
    context: &Ctx,
    options: &DocumentOptions,
    configure: &dyn Fn(&mut minijinja::Environment<'static>, &Pictures),
) -> Result<Vec<u8>, String> {
    let mut core = core_for(template, options);
    let pictures = Pictures::default();
    let mut env = crate::environment(&core, options.autoescape);
    add_string_filter(&mut env, &pictures);
    configure(&mut env, &pictures);
    install(&mut core, env, options.autoescape);
    core.host_part_scope = Some(picture_scope(Some(pictures.clone())));
    let entries = match context { Ctx::Map(entries) => entries.clone(), _ => Vec::new() };
    let self_reference = options.self_reference.clone();
    core.render(options.autoescape, &|core, _| {
        let entries = entries.iter().map(|(k, v)| (k.clone(), to_value(v, core, &pictures, false))).collect();
        Ok(Value::from_object(Root { entries, self_reference: self_reference.clone() }))
    })?;
    core.save_bytes()
}

/// Render a subdocument as docxtpl does before composing it: the default
/// environment, and pictures of the main template undefined.
pub fn render_subdocument(template: &[u8], context: &Ctx, options: &DocumentOptions) -> Result<Vec<u8>, String> {
    let mut core = core_for(template, options);
    core.host_part_scope = Some(picture_scope(None));
    let env = crate::environment(&core, options.autoescape);
    install(&mut core, env, options.autoescape);
    let pictures = Pictures::default();
    core.render(options.autoescape, &|core, _| Ok(to_value(context, core, &pictures, true)))?;
    core.save_bytes()
}
