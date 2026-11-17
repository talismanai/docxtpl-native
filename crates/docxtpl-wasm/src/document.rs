//! Render a DOCX with a prepared context (`value::Ctx`): pictures and
//! subdocuments become docxtpl deferred objects, filters can create pictures
//! while a part renders, and options select python-docx numbering compatibility.

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
}

fn deferred_image(image: &Image) -> Deferred {
    Deferred::Image { blob: image.bytes.clone(), info: None, filename: None, width: image.width, height: image.height, anchor: None, title: None, descr: None }
}

fn token_index(value: &Value) -> Option<usize> {
    if !value.is_safe() { return None; }
    let text = value.as_str()?;
    text.strip_prefix("\u{1}DTPLD")?.strip_suffix('\u{1}')?.parse().ok()
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
/// to the core after it renders, in the order their placeholders were issued.
fn picture_scope(pictures: Option<Pictures>) -> HostPartScope {
    HostPartScope(Arc::new(move |core, _part, execute| {
        if let Some(pictures) = &pictures {
            let mut state = pictures.0.lock().expect("pictures");
            state.base = core.deferred.len();
            state.pending.clear();
        }
        let result = execute(core);
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
