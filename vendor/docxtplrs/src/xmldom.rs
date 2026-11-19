//! Minimal XML DOM used for structured transformations (fix_tables, docPr ids,
//! rels/content-types editing). Namespace prefixes are treated literally, which
//! is sufficient for OOXML manipulation.

use quick_xml::events::{BytesDecl, Event};

use crate::xml_events::{reader as text_reader, TextRuns};

/// Apply constructor-time drawing IDs by expanded XML name, retaining other
/// events and restricting the main document to descendants of its body.
pub(crate) fn renumber_expanded_drawings(
    xml: &str, body_only: bool, mut next_docpr: Option<&mut u32>, mut next_picture: Option<&mut u32>,
) -> Result<String, String> {
    use quick_xml::events::BytesStart;
    use quick_xml::name::ResolveResult;
    use quick_xml::reader::NsReader;
    use quick_xml::Writer;

    const WORD_NS: &str = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
    const DRAW_NS: &str = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing";
    const PICTURE_NS: &str = "http://schemas.openxmlformats.org/drawingml/2006/picture";
    let mut reader = NsReader::from_str(xml);
    // A lone `&` stays text, as in quick-xml 0.36; the writer copies text
    // pieces and references back verbatim.
    reader.config_mut().allow_dangling_amp = true;
    let mut writer = Writer::new(Vec::with_capacity(xml.len()));
    let mut depth = 0usize;
    let mut body_depth = None;
    loop {
        let (namespace, event) = reader.read_resolved_event().map_err(|error| error.to_string())?;
        match event {
            Event::Start(ref element) | Event::Empty(ref element) => {
                let word = matches!(&namespace, ResolveResult::Bound(ns) if ns.into_inner() == WORD_NS);
                let draw = matches!(&namespace, ResolveResult::Bound(ns) if ns.into_inner() == DRAW_NS);
                let picture = matches!(&namespace, ResolveResult::Bound(ns) if ns.into_inner() == PICTURE_NS);
                if body_only && depth == 1 && word && element.local_name().into_inner() == "body" {
                    body_depth = Some(depth + 1);
                }
                let in_scope = if body_only { body_depth.is_some_and(|body| depth >= body) } else { depth > 0 };
                let next = if in_scope && draw && element.local_name().into_inner() == "docPr" {
                    next_docpr.as_deref_mut().map(|counter| { *counter += 1; *counter })
                } else if in_scope && picture && element.local_name().into_inner() == "cNvPr" {
                    next_picture.as_deref_mut().map(|counter| { let current = *counter; *counter += 1; current })
                } else { None };
                if let Some(id) = next {
                    let mut rewritten = BytesStart::new(element.name().into_inner());
                    let value = id.to_string();
                    let mut had_id = false;
                    for attr in element.attributes() {
                        let attr = attr.map_err(|error| error.to_string())?;
                        if attr.key.into_inner() == "id" {
                            rewritten.push_attribute(("id", value.as_str()));
                            had_id = true;
                        } else { rewritten.push_attribute(attr); }
                    }
                    if !had_id { rewritten.push_attribute(("id", value.as_str())); }
                    let rewritten = if matches!(event, Event::Empty(_)) { Event::Empty(rewritten) } else { Event::Start(rewritten) };
                    writer.write_event(rewritten).map_err(|error| error.to_string())?;
                } else { writer.write_event(event.borrow()).map_err(|error| error.to_string())?; }
                if matches!(event, Event::Start(_)) { depth += 1; }
                else if body_depth == Some(depth + 1) { body_depth = None; }
            }
            Event::End(_) => {
                depth = depth.checked_sub(1).ok_or("invalid XML depth")?;
                if body_depth.is_some_and(|body| depth < body) { body_depth = None; }
                writer.write_event(event).map_err(|error| error.to_string())?;
            }
            Event::Eof => break,
            _ => writer.write_event(event).map_err(|error| error.to_string())?,
        }
    }
    String::from_utf8(writer.into_inner()).map_err(|error| error.to_string())
}

/// Locate the document body without rewriting unrelated XML. The returned
/// standalone body carries namespace declarations inherited from the root.
pub(crate) fn document_body_source(xml: &str) -> Result<(String, std::ops::Range<usize>), String> {
    use quick_xml::name::ResolveResult;
    use quick_xml::reader::NsReader;

    const WORD_NS: &str = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
    let mut reader = NsReader::from_str(xml);
    reader.config_mut().allow_dangling_amp = true;
    let mut depth = 0usize;
    let mut namespaces: Vec<(String, String)> = Vec::new();
    let mut body_start = None;
    let mut insertion = None;
    let mut body_names: Vec<String> = Vec::new();
    loop {
        let before = reader.buffer_position() as usize;
        let (namespace, event) = reader.read_resolved_event().map_err(|error| error.to_string())?;
        match event {
            Event::Start(ref element) | Event::Empty(ref element) if depth == 0 => {
                for attribute in element.attributes() {
                    let attribute = attribute.map_err(|error| error.to_string())?;
                    let key = attribute.key.into_inner();
                    if key == "xmlns" || key.starts_with("xmlns:") {
                        namespaces.push((key.to_owned(), attribute.value.into_owned()));
                    }
                }
                if matches!(event, Event::Start(_)) { depth += 1; }
            }
            Event::Start(ref element) | Event::Empty(ref element)
                if depth == 1 && element.local_name().into_inner() == "body"
                    && matches!(namespace, ResolveResult::Bound(ns) if ns.into_inner() == WORD_NS) => {
                for attribute in element.attributes() {
                    let attribute = attribute.map_err(|error| error.to_string())?;
                    body_names.push(attribute.key.into_inner().to_owned());
                }
                body_start = Some(before);
                insertion = Some(before + 1 + element.name().into_inner().len());
                if matches!(event, Event::Empty(_)) {
                    let end = reader.buffer_position() as usize;
                    return finish_body_source(xml, before..end, insertion.unwrap(), &namespaces, &body_names);
                }
                depth += 1;
            }
            Event::Start(_) => depth += 1,
            Event::End(_) => {
                depth = depth.checked_sub(1).ok_or_else(|| "invalid XML depth".to_string())?;
                if depth == 1 {
                    if let (Some(start), Some(at)) = (body_start, insertion) {
                        let end = reader.buffer_position() as usize;
                        return finish_body_source(xml, start..end, at, &namespaces, &body_names);
                    }
                }
            }
            Event::Eof => break,
            _ => {}
        }
    }
    Err("word/document.xml has no w:body element".to_string())
}

fn finish_body_source(
    xml: &str,
    range: std::ops::Range<usize>,
    insertion: usize,
    namespaces: &[(String, String)],
    body_names: &[String],
) -> Result<(String, std::ops::Range<usize>), String> {
    let mut body = xml[range.start..insertion].to_owned();
    for (key, value) in namespaces {
        if body_names.contains(key) { continue; }
        body.push(' ');
        body.push_str(key);
        body.push_str("=\"");
        body.push_str(&value.replace('"', "&quot;"));
        body.push('"');
    }
    body.push_str(&xml[insertion..range.end]);
    Ok((body, range))
}

/// Namespace adoption performed by lxml's `moveNodeToDocument` when
/// docxtpl's `map_tree` replaces the document body with the rendered tree.
///
/// Walking the adopted subtree in document order, each namespace declaration
/// whose URI is already in scope from the element's parent, under any prefix
/// that is not shadowed, is removed; references made through it are renamed
/// to that prefix. As in lxml, the rename follows the declaration even when a
/// descendant later rebinds the target prefix. Declarations that would only
/// map onto a default namespace are kept: lxml additionally re-declares a
/// prefix for attributes in that case, which is not reproduced here.
pub(crate) fn adopt_body_namespaces(document: &str, body: &str) -> Option<String> {
    type Prefix = Option<String>;
    fn declarations(element: &quick_xml::events::BytesStart<'_>) -> Option<Vec<(Prefix, String)>> {
        let mut result = Vec::new();
        for attribute in element.attributes() {
            let attribute = attribute.ok()?;
            let key = attribute.key.into_inner();
            let prefix = if key == "xmlns" { None }
                else if let Some(prefix) = key.strip_prefix("xmlns:") { Some(prefix.to_owned()) }
                else { continue };
            result.push((prefix, quick_xml::escape::unescape(&attribute.value).ok()?.into_owned()));
        }
        Some(result)
    }
    fn split(name: &str) -> (Prefix, &str) {
        match name.split_once(':') {
            Some((prefix, local)) => (Some(prefix.to_owned()), local),
            None => (None, name),
        }
    }
    fn join(prefix: &Prefix, local: &str) -> String {
        match prefix { Some(prefix) => format!("{prefix}:{local}"), None => local.to_owned() }
    }
    struct Frame { kept: Vec<(Prefix, String)>, renamed: Vec<(Prefix, Prefix)> }
    fn rename(frames: &[Frame], prefix: &Prefix) -> Prefix {
        frames.iter().rev().find_map(|frame| frame.renamed.iter().find(|(from, _)| from == prefix))
            .map_or_else(|| prefix.clone(), |(_, to)| to.clone())
    }

    let mut source = text_reader(document);
    let root = loop {
        match source.read_event().ok()? {
            Event::Start(element) | Event::Empty(element) => break declarations(&element)?,
            Event::Eof => return None,
            _ => {}
        }
    };
    let mut frames: Vec<Frame> = Vec::new();
    let mut output = String::with_capacity(body.len());
    let mut copied = 0usize;
    let mut input = text_reader(body);
    loop {
        let before = input.buffer_position() as usize;
        let event = input.read_event().ok()?;
        let after = input.buffer_position() as usize;
        match event {
            Event::Start(ref element) | Event::Empty(ref element) => {
                let mut frame = Frame { kept: Vec::new(), renamed: Vec::new() };
                let mut changed = false;
                for (prefix, uri) in declarations(element)? {
                    // xmlSearchNsByHref from the parent, over declarations
                    // still present in the output; nearer prefixes shadow.
                    let mut seen: Vec<&Prefix> = Vec::new();
                    let parents = frames.iter().rev().flat_map(|frame| frame.kept.iter()).chain(root.iter());
                    let found = parents.filter_map(|(candidate, value)| {
                        if seen.contains(&candidate) { return None; }
                        seen.push(candidate);
                        (value == &uri).then_some(candidate)
                    }).next().cloned();
                    match found {
                        Some(target) if !uri.is_empty() && (target.is_some() || prefix.is_none()) => {
                            changed = true;
                            frame.renamed.push((prefix, target));
                        }
                        _ => {
                            frame.renamed.push((prefix.clone(), prefix.clone()));
                            frame.kept.push((prefix, uri));
                        }
                    }
                }
                frames.push(frame);
                let name = element.name().into_inner().to_owned();
                let (prefix, local) = split(&name);
                let renamed = join(&rename(&frames, &prefix), local);
                let mut tag = format!("<{renamed}");
                for attribute in element.attributes() {
                    let attribute = attribute.ok()?;
                    let key = attribute.key.into_inner();
                    let key = if key == "xmlns" || key.starts_with("xmlns:") {
                        let prefix = key.strip_prefix("xmlns:").map(str::to_owned);
                        if !frames.last()?.kept.iter().any(|(kept, _)| kept == &prefix) { continue; }
                        key.to_owned()
                    } else {
                        match split(key) {
                            (Some(prefix), local) if prefix != "xml" => {
                                let renamed = join(&rename(&frames, &Some(prefix)), local);
                                changed |= renamed != key;
                                renamed
                            }
                            _ => key.to_owned(),
                        }
                    };
                    let value = &*attribute.value;
                    let quote = if value.contains('"') { '\'' } else { '"' };
                    tag.push_str(&format!(" {key}={quote}{value}{quote}"));
                }
                if matches!(event, Event::Empty(_)) {
                    tag.push_str("/>");
                    frames.pop();
                } else { tag.push('>'); }
                if changed || renamed != name {
                    output.push_str(&body[copied..before]);
                    output.push_str(&tag);
                    copied = after;
                }
            }
            Event::End(ref element) => {
                let name = element.name().into_inner().to_owned();
                let (prefix, local) = split(&name);
                let renamed = join(&rename(&frames, &prefix), local);
                frames.pop()?;
                if renamed != name {
                    output.push_str(&body[copied..before]);
                    output.push_str(&format!("</{renamed}>"));
                    copied = after;
                }
            }
            Event::Eof => break,
            _ => {}
        }
    }
    output.push_str(&body[copied..]);
    Some(output)
}

/// Interned element/attribute name. Cloning is an Rc refcount bump, and
/// `Document::parse` deduplicates names through an intern table: a multi-MB
/// docx (tens of thousands of elements sharing a few dozen distinct names)
/// would otherwise allocate a fresh String per element and per attribute
/// key (~740k allocations on large renders, dominating xmldom parse time).
///
/// `Deref<Target = str>` and `PartialEq<&str>` keep existing call sites
/// (`el.name == "w:p"`, `el.name.starts_with(..)`, `format!("{}", el.name)`)
/// working unchanged.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Name(pub std::rc::Rc<str>);

impl Name {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl std::ops::Deref for Name {
    type Target = str;
    fn deref(&self) -> &str {
        &self.0
    }
}

impl std::borrow::Borrow<str> for Name {
    fn borrow(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Display for Name {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl From<&str> for Name {
    fn from(s: &str) -> Self {
        Name(std::rc::Rc::from(s))
    }
}

impl From<String> for Name {
    fn from(s: String) -> Self {
        Name(s.into())
    }
}

impl PartialEq<&str> for Name {
    fn eq(&self, other: &&str) -> bool {
        &*self.0 == *other
    }
}

impl PartialEq<Name> for &str {
    fn eq(&self, other: &Name) -> bool {
        *self == &*other.0
    }
}

impl PartialEq<&str> for &Name {
    fn eq(&self, other: &&str) -> bool {
        &***self as &str == *other
    }
}

/// Name dedup table used during parse.
#[derive(Default)]
struct Interner(std::collections::HashSet<std::rc::Rc<str>>);

impl Interner {
    fn intern(&mut self, s: &str) -> Name {
        if let Some(rc) = self.0.get(s) {
            return Name(rc.clone());
        }
        let rc: std::rc::Rc<str> = s.into();
        self.0.insert(rc.clone());
        Name(rc)
    }
}

#[derive(Debug, Clone)]
pub struct Element {
    pub name: Name,
    pub attrs: Vec<(Name, String)>,
    pub children: Vec<Node>,
}

#[derive(Debug, Clone)]
pub enum Node {
    Elem(Element),
    Text(String),
}

#[derive(Debug, Clone)]
pub struct Document {
    /// Everything before the root element (xml declaration etc.)
    pub prolog: String,
    pub root: Element,
}

fn escape_text(s: &str, out: &mut String) {
    // bulk-copy runs without special chars; all escaped chars are ASCII, so
    // byte indexing is char-safe
    let b = s.as_bytes();
    let mut last = 0usize;
    let mut i = 0usize;
    while i < b.len() {
        let rep = match b[i] {
            b'&' => "&amp;",
            b'<' => "&lt;",
            b'>' => "&gt;",
            _ => {
                i += 1;
                continue;
            }
        };
        out.push_str(&s[last..i]);
        out.push_str(rep);
        i += 1;
        last = i;
    }
    out.push_str(&s[last..]);
}

fn escape_attr(s: &str, out: &mut String) {
    let b = s.as_bytes();
    let mut last = 0usize;
    let mut i = 0usize;
    while i < b.len() {
        let rep = match b[i] {
            b'&' => "&amp;",
            b'<' => "&lt;",
            b'"' => "&quot;",
            _ => {
                i += 1;
                continue;
            }
        };
        out.push_str(&s[last..i]);
        out.push_str(rep);
        i += 1;
        last = i;
    }
    out.push_str(&s[last..]);
}

impl Element {
    pub fn new(name: &str) -> Self {
        Element {
            name: name.into(),
            attrs: Vec::new(),
            children: Vec::new(),
        }
    }

    pub fn get_attr(&self, name: &str) -> Option<&str> {
        self.attrs
            .iter()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.as_str())
    }

    pub fn set_attr(&mut self, name: &str, value: &str) {
        if let Some(slot) = self.attrs.iter_mut().find(|(k, _)| k == name) {
            slot.1 = value.to_string();
        } else {
            self.attrs.push((name.into(), value.to_string()));
        }
    }

    /// First direct child element with given name
    pub fn find(&self, name: &str) -> Option<&Element> {
        self.children.iter().find_map(|c| match c {
            Node::Elem(e) if e.name == name => Some(e),
            _ => None,
        })
    }

    pub fn find_mut(&mut self, name: &str) -> Option<&mut Element> {
        self.children.iter_mut().find_map(|c| match c {
            Node::Elem(e) if e.name == name => Some(e),
            _ => None,
        })
    }

    /// All direct child elements with given name
    pub fn find_all(&self, name: &str) -> Vec<&Element> {
        self.children
            .iter()
            .filter_map(|c| match c {
                Node::Elem(e) if e.name == name => Some(e),
                _ => None,
            })
            .collect()
    }

    /// Deep iteration over all descendant elements (including self) with given name.
    pub fn iter_descendants<'a>(&'a self, name: &'a str, out: &mut Vec<&'a Element>) {
        for c in &self.children {
            if let Node::Elem(e) = c {
                if e.name == name {
                    out.push(e);
                }
                e.iter_descendants(name, out);
            }
        }
    }

    pub fn serialize(&self, out: &mut String) {
        out.push('<');
        out.push_str(&self.name);
        for (k, v) in &self.attrs {
            out.push(' ');
            out.push_str(k);
            out.push_str("=\"");
            escape_attr(v, out);
            out.push('"');
        }
        if self.children.is_empty() {
            out.push_str("/>");
            return;
        }
        out.push('>');
        for c in &self.children {
            match c {
                Node::Elem(e) => e.serialize(out),
                Node::Text(t) => escape_text(t, out),
            }
        }
        out.push_str("</");
        out.push_str(&self.name);
        out.push('>');
    }

    /// Concatenated text content of all descendants
    pub fn text_content(&self) -> String {
        let mut s = String::new();
        self.push_text_content(&mut s);
        s
    }

    /// text_content without the intermediate String per element
    pub fn push_text_content(&self, out: &mut String) {
        for c in &self.children {
            match c {
                Node::Text(t) => out.push_str(t),
                Node::Elem(e) => e.push_text_content(out),
            }
        }
    }
}

impl Document {
    pub fn parse(xml: &str) -> Result<Document, String> {
        let mut reader = TextRuns::new(text_reader(xml));
        let mut prolog = String::new();
        let mut stack: Vec<Element> = Vec::new();
        let mut root: Option<Element> = None;
        let mut interner = Interner::default();

        loop {
            match reader.read_event().map_err(|e| e.to_string())? {
                Event::Start(e) => {
                    let name = interner.intern(e.name().into_inner());
                    let mut attrs = Vec::new();
                    for a in e.attributes() {
                        let a = a.map_err(|e| e.to_string())?;
                        let key = interner.intern(a.key.into_inner());
                        let val = quick_xml::escape::unescape(&a.value)
                            .map_err(|e| e.to_string())?
                            .into_owned();
                        attrs.push((key, val));
                    }
                    stack.push(Element {
                        name,
                        attrs,
                        children: Vec::new(),
                    });
                }
                Event::Empty(e) => {
                    let name = interner.intern(e.name().into_inner());
                    let mut attrs = Vec::new();
                    for a in e.attributes() {
                        let a = a.map_err(|e| e.to_string())?;
                        let key = interner.intern(a.key.into_inner());
                        let val = quick_xml::escape::unescape(&a.value)
                            .map_err(|e| e.to_string())?
                            .into_owned();
                        attrs.push((key, val));
                    }
                    let el = Element {
                        name,
                        attrs,
                        children: Vec::new(),
                    };
                    if let Some(parent) = stack.last_mut() {
                        parent.children.push(Node::Elem(el));
                    } else if root.is_none() {
                        root = Some(el);
                    }
                }
                Event::End(_) => {
                    if let Some(el) = stack.pop() {
                        if let Some(parent) = stack.last_mut() {
                            parent.children.push(Node::Elem(el));
                        } else {
                            root = Some(el);
                        }
                    }
                }
                Event::Text(e) => {
                    let raw: &str = &e;
                    // fast path: no '&' => nothing to unescape, keep the single
                    // allocation instead of copy + re-copy
                    let t = if raw.contains('&') {
                        match quick_xml::escape::unescape(raw) {
                            Ok(c) => c.into_owned(),
                            Err(_) => raw.to_owned(),
                        }
                    } else {
                        raw.to_owned()
                    };
                    if let Some(parent) = stack.last_mut() {
                        parent.children.push(Node::Text(t));
                    } else {
                        prolog.push_str(&t);
                    }
                }
                Event::CData(e) => {
                    let raw = e.into_inner().into_owned();
                    // keep CDATA content as raw text (will be escaped on serialize)
                    if let Some(parent) = stack.last_mut() {
                        parent.children.push(Node::Text(raw));
                    }
                }
                Event::Decl(e) => {
                    let s: &str = &e;
                    prolog.push_str("<?");
                    prolog.push_str(s);
                    prolog.push_str("?>");
                }
                Event::Comment(e) => {
                    let s: &str = &e;
                    let comment = format!("<!--{}-->", s);
                    if let Some(parent) = stack.last_mut() {
                        // preserve comments as unescaped text via placeholder? simpler: drop into prolog only if outside root
                        parent.children.push(Node::Text(String::new()));
                    } else {
                        prolog.push_str(&comment);
                    }
                }
                Event::PI(e) => {
                    let s: &str = &e;
                    let pi = format!("<?{}?>", s);
                    if stack.is_empty() {
                        prolog.push_str(&pi);
                    }
                }
                Event::DocType(e) => {
                    let s: &str = &e;
                    prolog.push_str("<!DOCTYPE");
                    prolog.push_str(s);
                    prolog.push('>');
                }
                Event::GeneralRef(_) => unreachable!("TextRuns joins references into text"),
                Event::Eof => break,
            }
        }

        let root = root.ok_or_else(|| "no root element found".to_string())?;
        Ok(Document { prolog, root })
    }

    pub fn serialize(&self) -> String {
        self.serialize_with_capacity(0)
    }

    /// Serialize with a preallocated output capacity. Pass the source xml
    /// length when known: a parse→serialize round-trip stays close in size,
    /// so this avoids the doubling-growth reallocs on multi-MB documents.
    pub fn serialize_with_capacity(&self, cap: usize) -> String {
        let mut out = String::with_capacity(cap);
        out.push_str(&self.prolog);
        self.root.serialize(&mut out);
        out
    }
}

/// Serialize only the `w:body` element of a wordprocessingml document part
/// (docxtpl get_xml returns `docx._element.body`). Like an lxml
/// serialization of a subtree, the namespace declarations in scope (from the
/// document root) are repeated on the body element so the result stays
/// well-formed on its own.
pub fn body_element_xml(xml: &str) -> Option<String> {
    let dom = Document::parse(xml).ok()?;
    let body = dom.root.find("w:body")?;
    let mut body = body.clone();
    let mut ns_attrs: Vec<(Name, String)> = Vec::new();
    for (k, v) in &dom.root.attrs {
        if k.as_str().starts_with("xmlns") && body.get_attr(k.as_str()).is_none() {
            ns_attrs.push((k.clone(), v.clone()));
        }
    }
    ns_attrs.append(&mut body.attrs);
    body.attrs = ns_attrs;
    let mut out = String::new();
    body.serialize(&mut out);
    Some(out)
}

/// Inner xml of the `w:body` element (body tags stripped) with any `w:sectPr`
/// removed (docxtpl Subdoc._get_xml).
pub fn body_inner_xml(xml: &str) -> Option<String> {
    let dom = Document::parse(xml).ok()?;
    let body = dom.root.find("w:body")?;
    let mut out = String::new();
    for c in &body.children {
        match c {
            Node::Elem(e) if e.name == "w:sectPr" => {}
            Node::Elem(e) => e.serialize(&mut out),
            Node::Text(t) => escape_text(t, &mut out),
        }
    }
    Some(out)
}

/// A new XML declaration helper (unused warnings guard)
#[allow(dead_code)]
pub fn decl(version: &str, encoding: &str, standalone: bool) -> String {
    let d = BytesDecl::new(version, Some(encoding), if standalone { Some("yes") } else { None });
    format!("<?{}?>", &*d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constructor_drawing_ids_follow_expanded_names_and_body_scope() {
        let xml = r#"<document xmlns="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:d="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:p="http://schemas.openxmlformats.org/drawingml/2006/picture"><d:docPr id="90"/><body><d:docPr id="9"/><p:cNvPr id="8"/><d:docPr xmlns:d="urn:wrong" id="7"/><cNvPr xmlns="http://schemas.openxmlformats.org/drawingml/2006/picture" id="6"/></body><p:cNvPr id="80"/></document>"#;
        let mut drawing = 0;
        let mut picture = 1;
        let output = renumber_expanded_drawings(xml, true, Some(&mut drawing), Some(&mut picture)).unwrap();
        assert!(output.contains(r#"<d:docPr id="90"/>"#));
        assert!(output.contains(r#"<d:docPr id="1"/>"#));
        assert!(output.contains(r#"<p:cNvPr id="1"/>"#));
        assert!(output.contains(r#"xmlns:d="urn:wrong" id="7""#));
        assert!(output.contains(r#"id="2"/>"#));
        assert!(output.contains(r#"<p:cNvPr id="80"/>"#));
        assert_eq!((drawing, picture), (1, 3));
        let mut render_id = 1000;
        let rendered = renumber_expanded_drawings(&output, true, Some(&mut render_id), None).unwrap();
        assert!(rendered.contains(r#"<d:docPr id="1001"/>"#));
        assert!(rendered.contains(r#"<p:cNvPr id="1"/>"#));
        assert_eq!(render_id, 1001);
    }

    #[test]
    fn body_source_keeps_outer_xml_and_inherits_namespaces() {
        let xml = r#"<?xml version="1.0"?><x:document xmlns:x="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:q='urn:&quot;test'><!--outside--><x:body xmlns:q="own"><q:item/><!--inside--></x:body><?after ok?></x:document>"#;
        let (body, range) = document_body_source(xml).unwrap();
        assert_eq!(body, r#"<x:body xmlns:x="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:q="own"><q:item/><!--inside--></x:body>"#);
        assert_eq!(&xml[range], r#"<x:body xmlns:q="own"><q:item/><!--inside--></x:body>"#);
    }

    #[test]
    fn body_source_supports_default_namespace_and_empty_body() {
        let xml = r#"<document xmlns="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:q='a"b'><body/></document>"#;
        let (body, range) = document_body_source(xml).unwrap();
        assert_eq!(body, r#"<body xmlns="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:q="a&quot;b"/>"#);
        assert_eq!(&xml[range], "<body/>");
        assert!(document_body_source(r#"<document xmlns="urn:wrong"><body/></document>"#).is_err());
    }

    #[test]
    fn adopted_body_namespaces_match_lxml_move_node_to_document() {
        // Expected bodies were produced by lxml: root.replace(body, fromstring(body)).
        let parent = r#"<w:document xmlns:w="W" xmlns:w14="U" xmlns:wp14="D"><w:body/></w:document>"#;
        let cases = [
            (r#"<w:body xmlns:w="W" xmlns:w14="U" xmlns:wp14="D"><w:p/></w:body>"#, "<w:body><w:p/></w:body>"),
            (r#"<w:body xmlns:w="W"><w:p xmlns:wp14="U" x="1" wp14:b="2"/></w:body>"#, r#"<w:body><w:p x="1" w14:b="2"/></w:body>"#),
            (r#"<w:body xmlns:w="W" xmlns:w14="U"><w:p xmlns:wp14="U" w14:a="1" wp14:b="2"><wp14:c/></w:p></w:body>"#,
             r#"<w:body><w:p w14:a="1" w14:b="2"><w14:c/></w:p></w:body>"#),
            (r#"<w:body xmlns:w="W"><w:t xmlns:x="V"><w:r xmlns:y="V" y:a="1"><y:e/></w:r></w:t></w:body>"#,
             r#"<w:body><w:t xmlns:x="V"><w:r x:a="1"><x:e/></w:r></w:t></w:body>"#),
            (r#"<w:body xmlns:w="W"><w:t xmlns:w14="V"><w:r xmlns:b="U" b:a="1"/></w:t></w:body>"#,
             r#"<w:body><w:t xmlns:w14="V"><w:r xmlns:b="U" b:a="1"/></w:t></w:body>"#),
            (r#"<w:body xmlns:w="W"><e xmlns="U" a="1"><f/></e></w:body>"#, r#"<w:body><w14:e a="1"><w14:f/></w14:e></w:body>"#),
            (r#"<w:body xmlns:w="W" xmlns="U"><w:p xmlns:z="U" z:a="1"/></w:body>"#, r#"<w:body><w:p w14:a="1"/></w:body>"#),
            (r#"<w:body xmlns:w="W"><w:p xmlns:k="U" k:a="1"><w:r xmlns:w14="Z" k:b="2" w14:c="3"/></w:p></w:body>"#,
             r#"<w:body><w:p w14:a="1"><w:r xmlns:w14="Z" w14:b="2" w14:c="3"/></w:p></w:body>"#),
            (r#"<w:body xmlns:w="W"><w:p xmlns:q="D"/></w:body>"#, "<w:body><w:p/></w:body>"),
            (r#"<w:body xmlns:w="W" xmlns:x="L" attr='a"b'/>"#, r#"<w:body xmlns:x="L" attr='a"b'/>"#),
        ];
        for (body, expected) in cases {
            assert_eq!(adopt_body_namespaces(parent, body).unwrap(), expected, "{body}");
        }
    }

    fn el(name: &str) -> Element {
        Element::new(name)
    }

    // ---------- Element::new / get_attr / set_attr ----------

    #[test]
    fn test_new_element_has_no_attrs_or_children() {
        let e = el("w:p");
        assert_eq!(e.name, "w:p");
        assert!(e.attrs.is_empty());
        assert!(e.children.is_empty());
    }

    #[test]
    fn test_get_attr_returns_none_for_missing() {
        let e = el("a");
        assert_eq!(e.get_attr("nope"), None);
    }

    #[test]
    fn test_set_attr_appends_new_attribute() {
        let mut e = el("a");
        e.set_attr("x", "1");
        e.set_attr("y", "2");
        assert_eq!(e.get_attr("x"), Some("1"));
        assert_eq!(e.get_attr("y"), Some("2"));
        // insertion order is preserved
        assert_eq!(
            e.attrs,
            vec![("x".into(), "1".to_string()), ("y".into(), "2".to_string())]
        );
    }

    #[test]
    fn test_set_attr_replaces_existing_in_place() {
        let mut e = el("a");
        e.set_attr("x", "1");
        e.set_attr("y", "2");
        e.set_attr("x", "3");
        assert_eq!(e.get_attr("x"), Some("3"));
        // no duplicate key added, order unchanged
        assert_eq!(e.attrs.len(), 2);
        assert_eq!(e.attrs[0].0, "x");
        assert_eq!(e.attrs[1].0, "y");
    }

    // ---------- find / find_mut / find_all / iter_descendants ----------

    #[test]
    fn test_find_returns_first_direct_child_only() {
        let mut root = el("root");
        let mut first = el("item");
        first.set_attr("id", "1");
        let mut second = el("item");
        second.set_attr("id", "2");
        root.children.push(Node::Text("text".into()));
        root.children.push(Node::Elem(first));
        root.children.push(Node::Elem(second));

        let found = root.find("item").expect("should find first item");
        assert_eq!(found.get_attr("id"), Some("1"));
        // text nodes are skipped, missing name returns None
        assert!(root.find("text").is_none());
    }

    #[test]
    fn test_find_does_not_recurse_into_grandchildren() {
        let mut root = el("root");
        let mut child = el("child");
        child.children.push(Node::Elem(el("item")));
        root.children.push(Node::Elem(child));
        assert!(root.find("item").is_none());
    }

    #[test]
    fn test_find_mut_allows_in_place_mutation() {
        let mut root = el("root");
        root.children.push(Node::Elem(el("item")));
        root.find_mut("item").unwrap().set_attr("id", "9");
        assert_eq!(root.find("item").unwrap().get_attr("id"), Some("9"));
        assert!(root.find_mut("missing").is_none());
    }

    #[test]
    fn test_find_all_returns_all_direct_matches_in_order() {
        let mut root = el("root");
        for id in ["a", "b", "c"] {
            let mut item = el("item");
            item.set_attr("id", id);
            root.children.push(Node::Elem(item));
        }
        root.children.push(Node::Elem(el("other")));
        let ids: Vec<&str> = root
            .find_all("item")
            .iter()
            .map(|e| e.get_attr("id").unwrap())
            .collect();
        assert_eq!(ids, vec!["a", "b", "c"]);
        assert!(root.find_all("nope").is_empty());
    }

    #[test]
    fn test_iter_descendants_collects_nested_matches() {
        let doc = Document::parse("<root><a><b/><a><b/></a></a><b/></root>").unwrap();
        let mut out = Vec::new();
        doc.root.iter_descendants("b", &mut out);
        assert_eq!(out.len(), 3);
        // self is not included even if the name matches
        let mut out = Vec::new();
        doc.root.iter_descendants("root", &mut out);
        assert!(out.is_empty());
    }

    // ---------- Element::serialize escaping ----------

    #[test]
    fn test_serialize_escapes_text_special_chars() {
        let mut e = el("t");
        e.children.push(Node::Text(r#"a<b>c&d"e'f"#.into()));
        let mut out = String::new();
        e.serialize(&mut out);
        // text escapes & < >, but not quotes
        assert_eq!(out, r#"<t>a&lt;b&gt;c&amp;d"e'f</t>"#);
    }

    #[test]
    fn test_serialize_escapes_attr_special_chars() {
        let mut e = el("t");
        e.set_attr("v", r#"a<b&c"d>e'f"#);
        let mut out = String::new();
        e.serialize(&mut out);
        // attrs escape & < ", but not > or '
        assert_eq!(out, r#"<t v="a&lt;b&amp;c&quot;d>e'f"/>"#);
    }

    #[test]
    fn test_serialize_empty_element_is_self_closing() {
        let e = el("w:br");
        let mut out = String::new();
        e.serialize(&mut out);
        assert_eq!(out, "<w:br/>");
    }

    #[test]
    fn test_serialize_nested_structure_and_attr_order() {
        let mut root = el("w:tc");
        root.set_attr("w:val", "x");
        let mut p = el("w:p");
        p.children.push(Node::Text("hi".into()));
        root.children.push(Node::Elem(p));
        let mut out = String::new();
        root.serialize(&mut out);
        assert_eq!(out, r#"<w:tc w:val="x"><w:p>hi</w:p></w:tc>"#);
    }

    // ---------- Document::parse / serialize round-trip ----------

    #[test]
    fn test_parse_roundtrip_simple_document() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><root a="1"><child>text</child></root>"#;
        let doc = Document::parse(xml).unwrap();
        assert_eq!(
            doc.prolog,
            r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>"#
        );
        assert_eq!(doc.root.name, "root");
        assert_eq!(doc.serialize(), xml);
    }

    #[test]
    fn test_parse_text_entities_are_decoded() {
        let doc = Document::parse("<t>a&lt;b&gt;c&amp;d&quot;e&apos;f</t>").unwrap();
        assert_eq!(doc.root.text_content(), "a<b>c&d\"e'f");
    }

    #[test]
    fn test_parse_numeric_char_references_are_decoded() {
        let doc = Document::parse("<t>&#65;&#x42;</t>").unwrap();
        assert_eq!(doc.root.text_content(), "AB");
    }

    #[test]
    fn test_parse_entity_roundtrip_in_text() {
        let xml = "<t>a&lt;b&amp;c</t>";
        let doc = Document::parse(xml).unwrap();
        assert_eq!(doc.serialize(), xml);
    }

    #[test]
    fn test_parse_attr_entities_are_decoded_and_reescaped() {
        let doc = Document::parse(r#"<t a="&lt;&amp;&quot;&gt;&apos;"/>"#).unwrap();
        assert_eq!(doc.root.get_attr("a"), Some("<&\">'"));
        // serialize re-escapes " as &quot;, but ' and > stay literal
        assert_eq!(doc.serialize(), r#"<t a="&lt;&amp;&quot;>'"/>"#);
    }

    #[test]
    fn test_parse_namespace_prefixes_treated_literally() {
        let xml = r#"<w:document xmlns:w="urn:x"><w:body w:rsidR="00AB"/></w:document>"#;
        let doc = Document::parse(xml).unwrap();
        assert_eq!(doc.root.name, "w:document");
        assert_eq!(doc.root.get_attr("xmlns:w"), Some("urn:x"));
        let body = doc.root.find("w:body").unwrap();
        assert_eq!(body.get_attr("w:rsidR"), Some("00AB"));
        assert_eq!(doc.serialize(), xml);
    }

    #[test]
    fn test_parse_self_closing_and_explicit_empty_tags() {
        let doc = Document::parse("<root><a/><b></b></root>").unwrap();
        assert!(doc.root.find("a").unwrap().children.is_empty());
        assert!(doc.root.find("b").unwrap().children.is_empty());
        // <b></b> has no children, so it serializes back self-closed
        assert_eq!(doc.serialize(), "<root><a/><b/></root>");
    }

    #[test]
    fn test_parse_deeply_nested_structure() {
        let xml = "<a><b><c><d>deep</d></c></b></a>";
        let doc = Document::parse(xml).unwrap();
        let d = doc
            .root
            .find("b")
            .and_then(|b| b.find("c"))
            .and_then(|c| c.find("d"))
            .unwrap();
        assert_eq!(d.text_content(), "deep");
        assert_eq!(doc.serialize(), xml);
    }

    #[test]
    fn test_parse_cdata_content_kept_as_raw_text() {
        let doc = Document::parse("<t><![CDATA[a<b&c]]></t>").unwrap();
        assert_eq!(doc.root.text_content(), "a<b&c");
        // CDATA markers are lost; content re-serializes as escaped text
        assert_eq!(doc.serialize(), "<t>a&lt;b&amp;c</t>");
    }

    #[test]
    fn test_parse_comment_before_root_goes_to_prolog() {
        let doc = Document::parse("<!--note--><root/>").unwrap();
        assert_eq!(doc.prolog, "<!--note-->");
        assert_eq!(doc.serialize(), "<!--note--><root/>");
    }

    #[test]
    fn test_parse_comment_inside_root_is_dropped() {
        let doc = Document::parse("<root><!--note--><a/></root>").unwrap();
        // comment inside root becomes an empty text node
        assert_eq!(doc.serialize(), "<root><a/></root>");
    }

    #[test]
    fn test_parse_pi_before_root_goes_to_prolog() {
        let doc = Document::parse("<?xml-stylesheet href=\"x\"?><root/>").unwrap();
        assert_eq!(doc.prolog, r#"<?xml-stylesheet href="x"?>"#);
        assert_eq!(doc.serialize(), r#"<?xml-stylesheet href="x"?><root/>"#);
    }

    #[test]
    fn test_parse_doctype_goes_to_prolog() {
        let doc = Document::parse("<!DOCTYPE html><root/>").unwrap();
        // NOTE: quick-xml strips the leading whitespace of the doctype content and
        // parse() rejoins without a space, so "<!DOCTYPE html>" does not round-trip
        assert_eq!(doc.prolog, "<!DOCTYPEhtml>");
        assert_eq!(doc.serialize(), "<!DOCTYPEhtml><root/>");
    }

    #[test]
    fn test_parse_whitespace_text_between_elements_preserved() {
        let xml = "<root> <a/> </root>";
        let doc = Document::parse(xml).unwrap();
        assert_eq!(doc.serialize(), xml);
    }

    #[test]
    fn test_text_content_concatenates_all_descendants() {
        let doc = Document::parse("<r>x<a>y</a>z<b><c>w</c></b></r>").unwrap();
        assert_eq!(doc.root.text_content(), "xyzw");
    }

    // ---------- error / edge paths ----------

    #[test]
    fn test_parse_mismatched_end_tag_errors() {
        let err = Document::parse("<a></b>").unwrap_err();
        assert!(!err.is_empty());
    }

    #[test]
    fn test_parse_unclosed_tag_errors_or_no_root() {
        // either quick-xml reports ill-formed, or the stack is never popped
        assert!(Document::parse("<a>").is_err());
    }

    #[test]
    fn test_parse_empty_input_errors_no_root() {
        let err = Document::parse("").unwrap_err();
        assert_eq!(err, "no root element found");
    }

    #[test]
    fn test_parse_prolog_only_errors_no_root() {
        let err = Document::parse(r#"<?xml version="1.0"?>"#).unwrap_err();
        assert_eq!(err, "no root element found");
    }

    #[test]
    fn test_parse_bare_text_errors_no_root() {
        assert!(Document::parse("not xml").is_err());
    }

    #[test]
    fn test_parse_unterminated_attr_value_errors() {
        assert!(Document::parse("<a x=\"1>").is_err());
    }

    #[test]
    fn test_parse_second_root_is_silently_dropped() {
        // documents current lenient behavior: only the first root is kept
        let doc = Document::parse("<a/><b/>").unwrap();
        assert_eq!(doc.root.name, "a");
        assert_eq!(doc.serialize(), "<a/>");
    }

    // ---------- decl helper ----------

    #[test]
    fn test_decl_standalone_yes() {
        assert_eq!(
            decl("1.0", "UTF-8", true),
            r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>"#
        );
    }

    #[test]
    fn test_decl_without_standalone() {
        assert_eq!(decl("1.0", "UTF-8", false), r#"<?xml version="1.0" encoding="UTF-8"?>"#);
    }

    #[test]
    fn test_body_element_xml_carries_namespaces() {
        let xml = "<?xml version=\"1.0\"?><w:document xmlns:w=\"urn:w\" xmlns:r=\"urn:r\"><w:body><w:p/></w:body></w:document>";
        let out = body_element_xml(xml).unwrap();
        assert_eq!(
            out,
            "<w:body xmlns:w=\"urn:w\" xmlns:r=\"urn:r\"><w:p/></w:body>"
        );
        assert!(body_element_xml("<w:document/>").is_none());
    }

    #[test]
    fn test_body_inner_xml_strips_body_and_sectpr() {
        let xml = "<w:document xmlns:w=\"urn:w\"><w:body><w:p><w:r><w:t>hi</w:t></w:r></w:p><w:sectPr><w:pgSz/></w:sectPr></w:body></w:document>";
        let out = body_inner_xml(xml).unwrap();
        assert_eq!(out, "<w:p><w:r><w:t>hi</w:t></w:r></w:p>");
    }
}

/// References in text and attribute values give the results quick-xml 0.36
/// gave: one text node per run of character data, resolved as a whole, or
/// kept as written when any reference in it cannot be resolved.
#[cfg(test)]
mod reference_tests {
    use super::*;

    fn children(xml: &str) -> Vec<String> {
        let doc = Document::parse(xml).unwrap();
        doc.root.children.iter().map(|node| match node {
            Node::Text(text) => format!("text {text}"),
            Node::Elem(element) => format!("element {}", element.name),
        }).collect()
    }

    #[test]
    fn a_run_with_references_is_one_resolved_text_node() {
        assert_eq!(children("<r>&amp;</r>"), ["text &"]);
        assert_eq!(children("<r>a &amp; b&lt;&gt;&quot;&apos;c</r>"), ["text a & b<>\"'c"]);
        assert_eq!(children("<r>x&#38;&#x26;&#60;&#x1D11E;y</r>"), ["text x&&<\u{1D11E}y"]);
        assert_eq!(children("<r>&lt;<b/>&amp;x<c/>&gt;</r>"), ["text <", "element b", "text &x", "element c", "text >"]);
        assert_eq!(children("<r>a&amp;<!--c-->&lt;b</r>"), ["text a&", "text ", "text <b"]);
        assert_eq!(children("<r>&amp;<![CDATA[&amp;]]>&lt;</r>"), ["text &", "text &amp;", "text <"]);
    }

    #[test]
    fn a_run_with_an_unresolvable_reference_keeps_its_source() {
        assert_eq!(children("<r>R&D</r>"), ["text R&D"]);
        assert_eq!(children("<r>&amp; &foo;</r>"), ["text &amp; &foo;"]);
        assert_eq!(children("<r>&#X41;&amp;</r>"), ["text &#X41;&amp;"]);
        assert_eq!(children("<r>&#0;</r>"), ["text &#0;"]);
        assert_eq!(children("<r>&amp</r>"), ["text &amp"]);
        assert_eq!(children("<r>&a&b;</r>"), ["text &a&b;"]);
        assert_eq!(children("<r>&lt;<b/>&</r>"), ["text <", "element b", "text &"]);
    }

    #[test]
    fn attribute_references_resolve_and_whitespace_is_kept() {
        let doc = Document::parse("<r a=\"&amp;&#38;&#x26;&lt;\" b='x\ty\nz&#10;&#9;'/>").unwrap();
        assert_eq!(doc.root.get_attr("a"), Some("&&&<"));
        assert_eq!(doc.root.get_attr("b"), Some("x\ty\nz\n\t"));
        assert!(Document::parse("<r a=\"&foo;\"/>").is_err());
        assert!(Document::parse("<r a=\"R&D\"/>").is_err());
    }

    #[test]
    fn references_survive_a_round_trip() {
        let xml = "<?xml version=\"1.0\"?>\n<r a=\"&quot;&amp;\">a&amp;b<c/>&lt;&#38;<d/>R&D</r>";
        assert_eq!(Document::parse(xml).unwrap().serialize(), "<?xml version=\"1.0\"?>\n<r a=\"&quot;&amp;\">a&amp;b<c/>&lt;&amp;<d/>R&amp;D</r>");
        // A run that cannot be resolved is kept as written, so its resolvable
        // references are escaped again on output.
        let xml = "<r>&lt;&#38;R&D</r>";
        assert_eq!(Document::parse(xml).unwrap().serialize(), "<r>&amp;lt;&amp;#38;R&amp;D</r>");
    }

    #[test]
    fn rewriting_passes_copy_text_with_references_verbatim() {
        let xml = r#"<document xmlns="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:d="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"><body><t>a &amp; b&#38;R&D&</t><d:docPr id="9" name="&lt;x&gt;"/>&lt;</body></document>"#;
        let mut drawing = 0;
        let output = renumber_expanded_drawings(xml, true, Some(&mut drawing), None).unwrap();
        assert_eq!(output, xml.replace(r#"id="9""#, r#"id="1""#));
        let (body, _) = document_body_source(xml).unwrap();
        assert!(body.ends_with(r#"><t>a &amp; b&#38;R&D&</t><d:docPr id="9" name="&lt;x&gt;"/>&lt;</body>"#), "{body}");
        let parent = r#"<w:document xmlns:w="W"><w:body/></w:document>"#;
        let adopted = adopt_body_namespaces(parent, r#"<w:body xmlns:w="W"><w:t>R&D &amp;</w:t></w:body>"#).unwrap();
        assert_eq!(adopted, "<w:body><w:t>R&D &amp;</w:t></w:body>");
    }
}
