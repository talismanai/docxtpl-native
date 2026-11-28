//! Render contexts with docxtpl objects: JSON values plus `InlineImage`,
//! `RichText` (run XML) and subdocuments, as a host prepares them before
//! rendering.

use std::fmt;
use std::sync::Arc;

use minijinja::value::{Object, ObjectRepr, Value};
use serde_json::Value as Json;

/// A context value as the template engine receives it.
#[derive(Debug, Clone)]
pub enum Ctx {
    Null,
    Bool(bool),
    Number(serde_json::Number),
    Str(String),
    /// docxtpl `RichText`: run XML inserted by `{{r }}`.
    Rich(String),
    Image(Image),
    /// A DOCX inserted as a docxtpl `Subdoc`.
    Subdoc(Arc<[u8]>),
    List(Vec<Ctx>),
    Map(Vec<(String, Ctx)>),
}

/// docxtpl `InlineImage`: picture bytes and optional EMU dimensions.
#[derive(Debug, Clone)]
pub struct Image {
    pub bytes: Arc<[u8]>,
    pub width: Option<i64>,
    pub height: Option<i64>,
}

impl Ctx {
    pub fn from_json(value: &Json) -> Ctx {
        match value {
            Json::Null => Ctx::Null,
            Json::Bool(b) => Ctx::Bool(*b),
            Json::Number(n) => Ctx::Number(n.clone()),
            Json::String(s) => Ctx::Str(s.clone()),
            Json::Array(items) => Ctx::List(items.iter().map(Ctx::from_json).collect()),
            Json::Object(map) => Ctx::Map(map.iter().map(|(k, v)| (k.clone(), Ctx::from_json(v))).collect()),
        }
    }

    pub fn get(&self, key: &str) -> Option<&Ctx> {
        match self { Ctx::Map(entries) => entries.iter().find(|(k, _)| k == key).map(|(_, v)| v), _ => None }
    }

    pub fn is_rich(&self) -> bool { matches!(self, Ctx::Rich(_)) }

    /// Python `==` between this value and JSON data (`True == 1`).
    pub fn py_eq(&self, data: &Json) -> bool {
        let number = |n: &serde_json::Number| n.as_f64();
        match (self, data) {
            (Ctx::Str(a), Json::String(b)) => a == b,
            (Ctx::Null, Json::Null) => true,
            (Ctx::Bool(a), Json::Bool(b)) => a == b,
            (Ctx::Bool(a), Json::Number(b)) => number(b) == Some(f64::from(u8::from(*a))),
            (Ctx::Number(a), Json::Bool(b)) => number(a) == Some(f64::from(u8::from(*b))),
            (Ctx::Number(a), Json::Number(b)) => number(a) == number(b),
            (Ctx::List(a), Json::Array(b)) => a.len() == b.len() && a.iter().zip(b).all(|(x, y)| x.py_eq(y)),
            (Ctx::Map(a), Json::Object(b)) => a.len() == b.len() && a.iter().all(|(k, v)| b.get(k).is_some_and(|w| v.py_eq(w))),
            _ => false,
        }
    }
}

/// Python `html.escape(text, quote=True)`.
pub fn html_escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for ch in text.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            c => out.push(c),
        }
    }
    out
}

/// Run properties of docxtpl 0.17 `RichText.add` that `rich_run` writes.
#[derive(Default, Clone)]
pub struct RunStyle<'a> {
    pub color: Option<&'a str>,
    pub highlight: Option<&'a str>,
    pub url_id: Option<&'a str>,
}

/// docxtpl 0.17 `RichText.add(text, **style)` XML for one run.
pub fn rich_run(text: &str, style: &RunStyle<'_>) -> String {
    let mut properties = String::new();
    if let Some(color) = style.color.filter(|c| !c.is_empty()) {
        properties.push_str(&format!("<w:color w:val=\"{}\"/>", color.strip_prefix('#').unwrap_or(color)));
    }
    if let Some(highlight) = style.highlight.filter(|h| !h.is_empty()) {
        properties.push_str(&format!("<w:shd w:fill=\"{}\"/>", highlight.strip_prefix('#').unwrap_or(highlight)));
    }
    let mut xml = String::from("<w:r>");
    if !properties.is_empty() { xml.push_str(&format!("<w:rPr>{properties}</w:rPr>")); }
    xml.push_str(&format!("<w:t xml:space=\"preserve\">{}</w:t></w:r>", html_escape(text)));
    match style.url_id.filter(|id| !id.is_empty()) {
        Some(id) => format!("<w:hyperlink r:id=\"{id}\" w:tgtFrame=\"_blank\">{xml}</w:hyperlink>"),
        None => xml,
    }
}

/// docxtpl `RichText` as a template sees it: an object whose `xml` is its run
/// XML. It prints that XML, unescaped as its `__html__` gives it, and `|e`
/// keeps it markup. It is not a string (`is string` is false; `== 'text'` is
/// false) and it is always true, even empty, as Python objects without
/// `__bool__` or `__len__` are.
#[derive(Debug)]
pub struct RichText {
    pub xml: String,
}

impl Object for RichText {
    fn repr(self: &Arc<Self>) -> ObjectRepr { ObjectRepr::Plain }

    fn get_value(self: &Arc<Self>, key: &Value) -> Option<Value> {
        (key.as_str()? == "xml").then(|| Value::from(self.xml.as_str()))
    }

    fn is_true(self: &Arc<Self>) -> bool { true }

    fn render(self: &Arc<Self>, f: &mut fmt::Formatter<'_>) -> fmt::Result { f.write_str(&self.xml) }
}

impl RichText {
    pub fn value(xml: &str) -> Value { Value::from_object(RichText { xml: xml.to_owned() }) }
}

/// The run XML of a [`RichText`] value.
pub fn rich_xml(value: &Value) -> Option<&str> {
    value.downcast_object_ref::<RichText>().map(|rich| rich.xml.as_str())
}
