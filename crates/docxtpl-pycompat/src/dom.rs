//! A mutable XML tree with the lxml semantics the template pipeline relies on.
//!
//! Nodes live in an arena with parent links. Text between elements is kept as
//! separate nodes; lxml stores it as the preceding element's `tail`, so
//! removing an element also removes the text nodes that follow it, and
//! sibling navigation (`getnext`) skips text but not comments or processing
//! instructions. Namespaces are resolved at parse time; prefixes and `xmlns`
//! attributes are kept for serialization.

use std::borrow::Cow;

use quick_xml::Reader;
use quick_xml::events::{BytesStart, BytesText, Event};

pub type NodeId = usize;

pub const W: &str = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
pub const XML_NS: &str = "http://www.w3.org/XML/1998/namespace";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Name {
    pub prefix: Option<String>,
    pub local: String,
    pub ns: Option<String>,
}

#[derive(Debug, Clone)]
pub struct Attr {
    pub name: Name,
    pub value: String,
}

#[derive(Debug, Clone)]
pub enum Kind {
    Document,
    Element { name: Name, attrs: Vec<Attr> },
    Text(String),
    CData(String),
    Comment(String),
    Pi(String),
}

#[derive(Debug, Clone)]
struct Node {
    kind: Kind,
    parent: Option<NodeId>,
    children: Vec<NodeId>,
}

#[derive(Debug, Clone)]
pub struct Dom {
    nodes: Vec<Node>,
    root: NodeId,
}

fn split_qname(qname: &str) -> (Option<String>, String) {
    match qname.split_once(':') {
        Some((prefix, local)) => (Some(prefix.to_owned()), local.to_owned()),
        None => (None, qname.to_owned()),
    }
}

fn escape_text(text: &str, out: &mut String) {
    for ch in text.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '\r' => out.push_str("&#13;"),
            c => out.push(c),
        }
    }
}

fn escape_attr(text: &str, out: &mut String) {
    for ch in text.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\n' => out.push_str("&#10;"),
            '\r' => out.push_str("&#13;"),
            '\t' => out.push_str("&#9;"),
            c => out.push(c),
        }
    }
}

/// Events with every run of character data between two markup events as one
/// `Event::Text` holding the run's source text, escapes included.
///
/// Since quick-xml 0.38 a reference in character data is its own
/// `Event::GeneralRef` between `Event::Text` pieces, and a `&` that starts no
/// reference is a reader error unless `allow_dangling_amp` is set. The parser
/// below was written for quick-xml 0.36, which reported a whole run as one
/// text event and left unescaping, and rejecting a lone `&`, to
/// `BytesText::unescape`. Joining the pieces back and unescaping the run with
/// `quick_xml::escape::unescape` keeps its results and its error messages.
/// `vendor/docxtplrs/src/xml_events.rs` does the same for that crate.
struct TextRuns<'i> {
    reader: Reader<&'i [u8]>,
    held: Option<quick_xml::Result<Event<'i>>>,
}

impl<'i> TextRuns<'i> {
    fn new(xml: &'i str) -> Self {
        let mut reader = Reader::from_str(xml);
        reader.config_mut().allow_dangling_amp = true;
        TextRuns { reader, held: None }
    }

    fn read_event(&mut self) -> quick_xml::Result<Event<'i>> {
        if let Some(held) = self.held.take() { return held; }
        let mut run: Option<Cow<'i, str>> = None;
        loop {
            let piece = match self.reader.read_event() {
                Ok(Event::Text(text)) => text.into_inner(),
                Ok(Event::GeneralRef(reference)) => Cow::Owned(format!("&{};", &*reference)),
                other => {
                    // The run ends at markup, at the end of input or at an
                    // error, which is reported after the text before it.
                    let Some(run) = run else { return other };
                    self.held = Some(other);
                    return Ok(Event::Text(BytesText::from_escaped(run)));
                }
            };
            if piece.is_empty() { continue; }
            run = Some(match run {
                None => piece,
                Some(mut run) => { run.to_mut().push_str(&piece); run }
            });
        }
    }
}

impl Dom {
    /// Parse a complete XML document.
    pub fn parse(xml: &str) -> Result<Dom, String> {
        let mut dom = Dom { nodes: vec![Node { kind: Kind::Document, parent: None, children: Vec::new() }], root: 0 };
        let mut reader = TextRuns::new(xml);
        reader.reader.config_mut().trim_text(false);
        reader.reader.config_mut().check_end_names = true;
        let mut stack: Vec<NodeId> = vec![dom.root];
        let mut scopes: Vec<Vec<(Option<String>, String)>> = vec![vec![(Some("xml".into()), XML_NS.into())]];
        loop {
            let event = reader.read_event().map_err(|error| format!("invalid XML: {error}"))?;
            match event {
                Event::Start(ref start) | Event::Empty(ref start) => {
                    let empty = matches!(event, Event::Empty(_));
                    let (name, attrs, scope) = Self::element_parts(start, &scopes)?;
                    let id = dom.push(Kind::Element { name, attrs }, *stack.last().expect("parent"));
                    if !empty { stack.push(id); scopes.push(scope); }
                }
                Event::End(_) => {
                    stack.pop();
                    scopes.pop();
                    if stack.is_empty() { return Err("unbalanced XML".into()); }
                }
                Event::Text(text) => {
                    let value = quick_xml::escape::unescape(&text).map_err(|error| format!("invalid XML text: {error}"))?.into_owned();
                    let parent = *stack.last().expect("parent");
                    if parent == dom.root && value.trim().is_empty() { continue; }
                    dom.push(Kind::Text(value), parent);
                }
                Event::CData(data) => {
                    let value = data.into_inner().into_owned();
                    dom.push(Kind::CData(value), *stack.last().expect("parent"));
                }
                Event::Comment(text) => {
                    let value = text.into_inner().into_owned();
                    dom.push(Kind::Comment(value), *stack.last().expect("parent"));
                }
                Event::PI(pi) => {
                    let value = pi.into_inner().into_owned();
                    dom.push(Kind::Pi(value), *stack.last().expect("parent"));
                }
                Event::Decl(_) => {}
                Event::DocType(_) => return Err("XML document type declarations are not supported".into()),
                Event::GeneralRef(_) => unreachable!("TextRuns joins references into text"),
                Event::Eof => break,
            }
        }
        if stack.len() != 1 { return Err("unclosed XML element".into()); }
        if dom.document_element().is_none() { return Err("missing XML root element".into()); }
        Ok(dom)
    }

    fn element_parts(start: &BytesStart<'_>, scopes: &[Vec<(Option<String>, String)>])
        -> Result<(Name, Vec<Attr>, Vec<(Option<String>, String)>), String> {
        let mut raw = Vec::new();
        let mut scope = Vec::new();
        for attribute in start.attributes() {
            let attribute = attribute.map_err(|error| format!("invalid XML attribute: {error}"))?;
            let key = attribute.key.into_inner().to_owned();
            let value = quick_xml::escape::unescape(&attribute.value)
                .map_err(|error| format!("invalid XML attribute value: {error}"))?.into_owned();
            if key == "xmlns" { scope.push((None, value.clone())); }
            else if let Some(prefix) = key.strip_prefix("xmlns:") { scope.push((Some(prefix.to_owned()), value.clone())); }
            raw.push((key, value));
        }
        let lookup = |prefix: &Option<String>| -> Option<String> {
            scope.iter().rev().chain(scopes.iter().rev().flat_map(|s| s.iter().rev()))
                .find(|(p, _)| p == prefix).map(|(_, uri)| uri.clone()).filter(|uri| !uri.is_empty())
        };
        let qname = start.name().into_inner().to_owned();
        let (prefix, local) = split_qname(&qname);
        let ns = lookup(&prefix);
        if prefix.is_some() && ns.is_none() { return Err(format!("unbound XML prefix in {qname}")); }
        let mut attrs = Vec::with_capacity(raw.len());
        for (key, value) in raw {
            let (prefix, local) = split_qname(&key);
            let ns = if key == "xmlns" || prefix.as_deref() == Some("xmlns") { None }
                else if prefix.is_some() { Some(lookup(&prefix).ok_or_else(|| format!("unbound XML prefix in {key}"))?) }
                else { None };
            attrs.push(Attr { name: Name { prefix, local, ns }, value });
        }
        let mut chain: Vec<(Option<String>, String)> = scopes.last().cloned().unwrap_or_default();
        chain.extend(scope);
        Ok((Name { prefix, local, ns }, attrs, chain))
    }

    fn push(&mut self, kind: Kind, parent: NodeId) -> NodeId {
        let id = self.nodes.len();
        self.nodes.push(Node { kind, parent: Some(parent), children: Vec::new() });
        self.nodes[parent].children.push(id);
        id
    }

    pub fn document_element(&self) -> Option<NodeId> {
        self.nodes[self.root].children.iter().copied().find(|&id| self.is_element(id))
    }

    pub fn kind(&self, id: NodeId) -> &Kind { &self.nodes[id].kind }

    pub fn is_element(&self, id: NodeId) -> bool { matches!(self.nodes[id].kind, Kind::Element { .. }) }

    pub fn name(&self, id: NodeId) -> Option<&Name> {
        match &self.nodes[id].kind { Kind::Element { name, .. } => Some(name), _ => None }
    }

    /// Element `{ns}local` (Clark notation match in lxml).
    pub fn is(&self, id: NodeId, ns: &str, local: &str) -> bool {
        self.name(id).is_some_and(|n| n.local == local && n.ns.as_deref() == Some(ns))
    }

    /// XPath `local-name() = 'local'` on an element.
    pub fn is_local(&self, id: NodeId, local: &str) -> bool { self.name(id).is_some_and(|n| n.local == local) }

    pub fn parent(&self, id: NodeId) -> Option<NodeId> {
        self.nodes[id].parent.filter(|&parent| parent != self.root)
    }

    /// lxml children: elements, comments and processing instructions.
    pub fn children(&self, id: NodeId) -> Vec<NodeId> {
        self.nodes[id].children.iter().copied().filter(|&c| !matches!(self.nodes[c].kind, Kind::Text(_) | Kind::CData(_))).collect()
    }

    /// lxml `getnext()`.
    pub fn next(&self, id: NodeId) -> Option<NodeId> {
        let parent = self.nodes[id].parent?;
        let siblings = &self.nodes[parent].children;
        let position = siblings.iter().position(|&c| c == id)?;
        siblings[position + 1..].iter().copied().find(|&c| !matches!(self.nodes[c].kind, Kind::Text(_) | Kind::CData(_)))
    }

    /// lxml `itersiblings()` (following siblings).
    pub fn following_siblings(&self, id: NodeId) -> Vec<NodeId> {
        let Some(parent) = self.nodes[id].parent else { return Vec::new() };
        let siblings = &self.nodes[parent].children;
        let position = siblings.iter().position(|&c| c == id).unwrap_or(siblings.len());
        siblings[position + 1..].iter().copied().filter(|&c| !matches!(self.nodes[c].kind, Kind::Text(_) | Kind::CData(_))).collect()
    }

    /// lxml `parent.index(child)` over element/comment/PI children.
    pub fn index(&self, parent: NodeId, child: NodeId) -> Option<usize> {
        self.children(parent).iter().position(|&c| c == child)
    }

    /// Descendant elements in document order (XPath `.//*`), excluding `id`.
    pub fn descendants(&self, id: NodeId) -> Vec<NodeId> {
        let mut out = Vec::new();
        let mut stack: Vec<NodeId> = self.nodes[id].children.iter().rev().copied().collect();
        while let Some(node) = stack.pop() {
            if self.is_element(node) { out.push(node); }
            stack.extend(self.nodes[node].children.iter().rev().copied());
        }
        out
    }

    /// Elements of the whole document in document order (`//*`).
    pub fn all_elements(&self) -> Vec<NodeId> { self.descendants(self.root) }

    pub fn has_descendant_local(&self, id: NodeId, local: &str) -> bool {
        self.descendants(id).into_iter().any(|d| self.is_local(d, local))
    }

    pub fn attr(&self, id: NodeId, ns: Option<&str>, local: &str) -> Option<&str> {
        match &self.nodes[id].kind {
            Kind::Element { attrs, .. } => attrs.iter().find(|a| a.name.local == local && a.name.ns.as_deref() == ns
                && a.name.prefix.as_deref() != Some("xmlns") && !(a.name.prefix.is_none() && a.name.local == "xmlns"))
                .map(|a| a.value.as_str()),
            _ => None,
        }
    }

    /// `elem.get(w:id) or elem.attrib.get("id", "")`.
    pub fn w_id(&self, id: NodeId) -> String {
        self.attr(id, Some(W), "id").filter(|v| !v.is_empty()).or_else(|| self.attr(id, None, "id")).unwrap_or_default().to_owned()
    }

    pub fn set_attr(&mut self, id: NodeId, name: Name, value: &str) {
        if let Kind::Element { attrs, .. } = &mut self.nodes[id].kind {
            match attrs.iter_mut().find(|a| a.name.local == name.local && a.name.ns == name.ns) {
                Some(existing) => existing.value = value.to_owned(),
                None => attrs.push(Attr { name, value: value.to_owned() }),
            }
        }
    }

    /// lxml `attrib.clear()`: namespace declarations are not attributes.
    pub fn clear_attrs(&mut self, id: NodeId) {
        if let Kind::Element { attrs, .. } = &mut self.nodes[id].kind {
            attrs.retain(|a| a.name.prefix.as_deref() == Some("xmlns") || (a.name.prefix.is_none() && a.name.local == "xmlns"));
        }
    }

    /// Concatenated text of descendant `w:t`-like nodes is computed by callers;
    /// this returns a text-only element's text (lxml `.text` of a leaf).
    pub fn text(&self, id: NodeId) -> Option<String> {
        let mut out = String::new();
        let mut any = false;
        for &child in &self.nodes[id].children {
            match &self.nodes[child].kind {
                Kind::Text(text) | Kind::CData(text) => { out.push_str(text); any = true; }
                _ => break,
            }
        }
        any.then_some(out)
    }

    /// Detach `id` together with its tail text (lxml `parent.remove`).
    pub fn remove(&mut self, id: NodeId) {
        let Some(parent) = self.nodes[id].parent else { return };
        let siblings = &self.nodes[parent].children;
        let Some(position) = siblings.iter().position(|&c| c == id) else { return };
        let mut end = position + 1;
        while end < siblings.len() && matches!(self.nodes[siblings[end]].kind, Kind::Text(_) | Kind::CData(_)) { end += 1; }
        let removed: Vec<NodeId> = self.nodes[parent].children.drain(position..end).collect();
        for node in removed { self.nodes[node].parent = None; }
    }

    /// Insert a detached node as lxml `parent.insert(index, node)`.
    pub fn insert(&mut self, parent: NodeId, index: usize, node: NodeId) {
        let children = self.children(parent);
        let position = match children.get(index) {
            Some(&before) => self.nodes[parent].children.iter().position(|&c| c == before).expect("child"),
            None => self.nodes[parent].children.len(),
        };
        self.nodes[parent].children.insert(position, node);
        self.nodes[node].parent = Some(parent);
    }

    pub fn append(&mut self, parent: NodeId, node: NodeId) {
        self.nodes[parent].children.push(node);
        self.nodes[node].parent = Some(parent);
    }

    pub fn new_element(&mut self, name: Name, attrs: Vec<Attr>) -> NodeId {
        let id = self.nodes.len();
        self.nodes.push(Node { kind: Kind::Element { name, attrs }, parent: None, children: Vec::new() });
        id
    }

    pub fn new_text(&mut self, text: &str) -> NodeId {
        let id = self.nodes.len();
        self.nodes.push(Node { kind: Kind::Text(text.to_owned()), parent: None, children: Vec::new() });
        id
    }

    /// Deep copy of `id` from `source` as a detached node of this tree
    /// (lxml `deepcopy`; the copy keeps no tail).
    pub fn import(&mut self, source: &Dom, id: NodeId) -> NodeId {
        let copy = self.nodes.len();
        self.nodes.push(Node { kind: source.nodes[id].kind.clone(), parent: None, children: Vec::new() });
        for &child in &source.nodes[id].children {
            let child_copy = self.import(source, child);
            self.append(copy, child_copy);
        }
        copy
    }

    pub fn deep_copy(&mut self, id: NodeId) -> NodeId {
        let snapshot = self.clone();
        self.import(&snapshot, id)
    }

    /// Namespace prefix in scope at `id` for `uri` (element ancestors only).
    pub fn prefix_for(&self, mut id: NodeId, uri: &str) -> Option<Option<String>> {
        loop {
            if let Kind::Element { attrs, .. } = &self.nodes[id].kind {
                for a in attrs {
                    if a.value == uri {
                        if a.name.prefix.as_deref() == Some("xmlns") { return Some(Some(a.name.local.clone())); }
                        if a.name.prefix.is_none() && a.name.local == "xmlns" { return Some(None); }
                    }
                }
            }
            id = self.nodes[id].parent?;
        }
    }

    /// Whitespace that lxml `pretty_print=True` writes, as text nodes, so a
    /// written-then-reparsed tree is reproduced (libxml `xmlsave.c` format
    /// mode: two-space indentation inside elements without text children;
    /// such an element disables formatting for its whole subtree).
    pub fn pretty_whitespace(&mut self) {
        if let Some(root) = self.document_element() { self.format(root, 0, true); }
    }

    fn format(&mut self, id: NodeId, level: usize, enabled: bool) {
        let children = self.nodes[id].children.clone();
        if children.is_empty() { return; }
        let has_text = children.iter().any(|&c| matches!(self.nodes[c].kind, Kind::Text(_) | Kind::CData(_)));
        let enabled = enabled && !has_text;
        for &child in &children {
            if self.is_element(child) { self.format(child, level + 1, enabled); }
        }
        if !enabled { return; }
        let inner = format!("\n{}", "  ".repeat(level + 1));
        let mut rebuilt = Vec::with_capacity(children.len() * 2 + 1);
        for &child in &children {
            let text = self.new_text(&inner);
            self.nodes[text].parent = Some(id);
            rebuilt.push(text);
            rebuilt.push(child);
        }
        let closing = self.new_text(&format!("\n{}", "  ".repeat(level)));
        self.nodes[closing].parent = Some(id);
        rebuilt.push(closing);
        // The indentation before a child belongs to the previous sibling's
        // tail; the closing indentation is the last child's tail.
        self.nodes[id].children = rebuilt;
    }

    /// python-docx/libxml `remove_blank_text` on load: whitespace-only text is
    /// dropped unless it is an element's whole content or under
    /// `xml:space="preserve"`.
    pub fn strip_blank_text(&mut self) {
        let ids: Vec<NodeId> = (0..self.nodes.len()).collect();
        for id in ids {
            let Kind::Text(text) = &self.nodes[id].kind else { continue };
            if !text.chars().all(|c| matches!(c, ' ' | '\t' | '\n' | '\r')) { continue; }
            let Some(parent) = self.nodes[id].parent else { continue };
            if parent == self.root { continue; }
            let sole = self.nodes[parent].children.iter().all(|&c| matches!(&self.nodes[c].kind, Kind::Text(t) if t.chars().all(|c| matches!(c, ' ' | '\t' | '\n' | '\r'))));
            if sole || self.preserves_space(parent) { continue; }
            self.nodes[parent].children.retain(|&c| c != id);
            self.nodes[id].parent = None;
        }
    }

    fn preserves_space(&self, mut id: NodeId) -> bool {
        loop {
            if let Some(value) = self.attr(id, Some(XML_NS), "space") { return value == "preserve"; }
            match self.nodes[id].parent { Some(parent) if parent != self.root => id = parent, _ => return false }
        }
    }

    pub fn serialize(&self) -> String {
        let mut out = String::from("<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n");
        if let Some(root) = self.document_element() { self.write(root, &mut out); }
        out
    }

    pub fn serialize_node(&self, id: NodeId) -> String {
        let mut out = String::new();
        self.write(id, &mut out);
        out
    }

    fn write(&self, id: NodeId, out: &mut String) {
        match &self.nodes[id].kind {
            Kind::Document => for &c in &self.nodes[id].children { self.write(c, out); },
            Kind::Text(text) => escape_text(text, out),
            Kind::CData(text) => { out.push_str("<![CDATA["); out.push_str(text); out.push_str("]]>"); }
            Kind::Comment(text) => { out.push_str("<!--"); out.push_str(text); out.push_str("-->"); }
            Kind::Pi(text) => { out.push_str("<?"); out.push_str(text); out.push_str("?>"); }
            Kind::Element { name, attrs } => {
                let qualified = |n: &Name| match &n.prefix { Some(p) => format!("{p}:{}", n.local), None => n.local.clone() };
                out.push('<');
                out.push_str(&qualified(name));
                for attr in attrs {
                    out.push(' ');
                    out.push_str(&qualified(&attr.name));
                    out.push_str("=\"");
                    escape_attr(&attr.value, out);
                    out.push('"');
                }
                if self.nodes[id].children.is_empty() { out.push_str("/>"); return; }
                out.push('>');
                for &c in &self.nodes[id].children { self.write(c, out); }
                out.push_str("</");
                out.push_str(&qualified(name));
                out.push('>');
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn remove_takes_tail_and_next_skips_text() {
        let mut dom = Dom::parse("<a><b/>tailb<c/>tailc<!--k--><d/></a>").unwrap();
        let root = dom.document_element().unwrap();
        let children = dom.children(root);
        assert!(matches!(dom.kind(dom.next(children[1]).unwrap()), Kind::Comment(_)));
        dom.remove(children[1]);
        assert_eq!(dom.serialize_node(root), "<a><b/>tailb<!--k--><d/></a>");
    }

    #[test]
    fn insert_counts_non_text_children() {
        let mut dom = Dom::parse("<a>head<b/>tb<c/></a>").unwrap();
        let root = dom.document_element().unwrap();
        let x = dom.new_element(Name { prefix: None, local: "x".into(), ns: None }, vec![]);
        dom.insert(root, 1, x);
        assert_eq!(dom.serialize_node(root), "<a>head<b/>tb<x/><c/></a>");
    }

    #[test]
    fn namespaces_resolve_and_roundtrip() {
        let xml = r#"<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p w:id="3">a &amp; b</w:p></w:body></w:document>"#;
        let dom = Dom::parse(xml).unwrap();
        let p = dom.all_elements().into_iter().find(|&e| dom.is(e, W, "p")).unwrap();
        assert_eq!(dom.w_id(p), "3");
        assert_eq!(dom.serialize_node(dom.document_element().unwrap()), xml);
    }
}

/// References in text and attribute values give the results and the error
/// messages quick-xml 0.36 gave.
#[cfg(test)]
mod reference_tests {
    use super::*;

    fn texts(xml: &str) -> Vec<String> {
        let dom = Dom::parse(xml).unwrap();
        let root = dom.document_element().unwrap();
        dom.nodes[root].children.iter().map(|&id| match dom.kind(id) {
            Kind::Text(text) => format!("text {text}"),
            Kind::CData(text) => format!("cdata {text}"),
            Kind::Comment(text) => format!("comment {text}"),
            Kind::Element { name, .. } => format!("element {}", name.local),
            other => format!("{other:?}"),
        }).collect()
    }

    #[test]
    fn a_run_with_references_is_one_resolved_text_node() {
        assert_eq!(texts("<r>&amp;</r>"), ["text &"]);
        assert_eq!(texts("<r>a &amp; b&lt;&gt;&quot;&apos;&#38;&#x26;&#x1D11E;c</r>"), ["text a & b<>\"'&&\u{1D11E}c"]);
        assert_eq!(texts("<r>&lt;<b/>&amp;x<!--k-->&gt;</r>"), ["text <", "element b", "text &x", "comment k", "text >"]);
        assert_eq!(texts("<r>&amp;<![CDATA[&amp;]]>&lt;</r>"), ["text &", "cdata &amp;", "text <"]);
        let dom = Dom::parse("<r>a&amp;b</r>").unwrap();
        assert_eq!(dom.serialize_node(dom.document_element().unwrap()), "<r>a&amp;b</r>");
    }

    #[test]
    fn an_unresolvable_text_reference_is_an_error() {
        assert_eq!(Dom::parse("<r>R&D</r>").unwrap_err(), "invalid XML text: Error while escaping character at range 1..3: Cannot find ';' after '&'");
        assert_eq!(Dom::parse("<r>&amp; &foo;</r>").unwrap_err(), "invalid XML text: at 7..10: unrecognized entity `foo`");
        assert_eq!(Dom::parse("<r>x&#0;</r>").unwrap_err(), "invalid XML text: invalid character reference: 0x0 character is not permitted in XML");
        assert_eq!(Dom::parse("<r>&amp</r>").unwrap_err(), "invalid XML text: Error while escaping character at range 0..4: Cannot find ';' after '&'");
    }

    #[test]
    fn attribute_references_resolve_and_whitespace_is_kept() {
        let dom = Dom::parse("<r a=\"&amp;&#38;&#x26;&lt;\" b='x\ty\nz&#10;&#9;'/>").unwrap();
        let root = dom.document_element().unwrap();
        assert_eq!(dom.attr(root, None, "a"), Some("&&&<"));
        assert_eq!(dom.attr(root, None, "b"), Some("x\ty\nz\n\t"));
        assert_eq!(Dom::parse("<r a=\"&foo;\"/>").unwrap_err(), "invalid XML attribute value: at 1..4: unrecognized entity `foo`");
    }

    #[test]
    fn blank_text_outside_the_root_is_dropped() {
        let dom = Dom::parse("<?xml version=\"1.0\"?>\n<r>&#32;</r>\n").unwrap();
        assert_eq!(dom.serialize_node(dom.document_element().unwrap()), "<r> </r>");
    }
}
