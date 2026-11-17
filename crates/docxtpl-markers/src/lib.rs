//! Link a place in a rendered document back to the template that produced
//! it, without changing how the document is laid out.
//!
//! A caller that wants to know where a template's output landed can wrap it
//! in named bookmarks while rendering. A *point* bookmark inside a line is
//! not free, though: it splits the line into separate text portions, and a
//! layout engine then stops shrinking the spaces of a justified line, so the
//! document with markers is laid out differently from the document without
//! them.
//!
//! [`anchor_inline_markers`] removes that difference. After the document is
//! fully rendered, every marker that sits inside a line is replaced by one
//! point bookmark at the start of its paragraph — shared by all of that
//! paragraph's markers — plus an [`InlineMarker`] recording how many
//! non-whitespace characters of the paragraph come before it. A converter
//! that reports where each bookmarked paragraph was laid out then gives the
//! paragraph, and the character count finds the spot inside it by aligning
//! the paragraph's text with the characters the converter laid out.
//!
//! Markers with nothing at all on one side of them (not even a space) do not
//! split a line and are left where they are.

use docxtpl_pycompat::dom::{Attr, Dom, Name, NodeId};
use docxtpl_pycompat::pyvalue::py_is_space;
use docxtplrs::package::Package;

const W: &str = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
const DOCUMENT: &str = "word/document.xml";
/// Run content that is laid out as a character but is not `w:t` text.
const LAID_OUT_CHARS: [&str; 6] = ["tab", "br", "cr", "sym", "noBreakHyphen", "softHyphen"];

/// How the markers and the anchors inserted for them are named.
///
/// `anchor_prefix` must start with `marker_prefix`, so that one search finds
/// both and an anchor is never mistaken for a marker.
#[derive(Debug, Clone)]
pub struct Naming {
    /// The prefix every marker bookmark's name starts with.
    pub marker_prefix: String,
    /// The prefix of the paragraph anchors this module inserts.
    pub anchor_prefix: String,
    /// The first id given to an inserted anchor. Keep it above every
    /// bookmark id the renderer itself uses.
    pub first_anchor_id: u64,
}

/// A marker that was inside a line, placed relative to its paragraph.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InlineMarker {
    /// Name of the point bookmark at the start of the marker's paragraph.
    pub paragraph_anchor: String,
    /// Non-whitespace characters of the paragraph before the marker.
    pub offset: usize,
    /// The paragraph's text with its whitespace removed.
    pub paragraph_text: String,
}

/// A marker to place by counting back from another one, for a marker the
/// render did not keep.
///
/// Jinja whitespace control is one way to lose one: `-%}` strips the XML up
/// to the next text and `{%-` back to the previous one, which can remove the
/// start of a value's paragraph, the marker in it included. The caller that
/// knows a start is missing asks for it `chars_before` non-whitespace
/// characters before the end marker that survived.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RelativeMarker {
    /// The marker to place.
    pub name: String,
    /// The marker to count back from.
    pub relative_to: String,
    /// How many non-whitespace characters before it.
    pub chars_before: usize,
}

/// A rendered document with its in-line markers moved to paragraph anchors.
#[derive(Debug, Clone, Default)]
pub struct Anchored {
    pub content: Vec<u8>,
    /// The in-line markers by name, in document order.
    pub inline_markers: Vec<(String, InlineMarker)>,
}

fn owning_paragraph(dom: &Dom, element: NodeId) -> Option<NodeId> {
    let mut parent = dom.parent(element);
    while let Some(node) = parent {
        if dom.is(node, W, "p") { return Some(node); }
        parent = dom.parent(node);
    }
    None
}

/// Whether the element's run is hidden text (`w:vanish`, not turned off):
/// it is in the XML but never laid out.
fn is_hidden(dom: &Dom, element: NodeId) -> bool {
    let Some(run) = dom.parent(element).filter(|&r| dom.is(r, W, "r")) else { return false };
    let Some(properties) = dom.children(run).into_iter().find(|&c| dom.is(c, W, "rPr")) else { return false };
    let Some(vanish) = dom.children(properties).into_iter().find(|&c| dom.is(c, W, "vanish")) else { return false };
    !matches!(dom.attr(vanish, Some(W), "val").unwrap_or("true"), "0" | "false" | "off")
}

/// What one paragraph's markers turned into.
struct ParagraphMarkers {
    /// In-line markers, removed from the paragraph.
    moved: Vec<(String, usize)>,
    /// Markers at either end of the paragraph, left in place.
    kept: Vec<(String, usize)>,
    /// The paragraph's text with its whitespace removed.
    text: String,
}

/// Remove the paragraph's in-line markers and return their names and
/// offsets, the offsets of the markers kept, and the paragraph's text.
fn move_inline_markers(dom: &mut Dom, paragraph: NodeId, naming: &Naming) -> ParagraphMarkers {
    let mut chunks = String::new();
    let mut offset = 0usize; // non-whitespace characters: what the alignment counts
    let mut laid_out = 0usize; // every character, spaces included: what layout sees
    let mut markers: Vec<(NodeId, usize, usize)> = Vec::new();
    for element in dom.descendants(paragraph) {
        let is_text = dom.is(element, W, "t");
        let is_bookmark = dom.is(element, W, "bookmarkStart");
        let laid_out_char = LAID_OUT_CHARS.iter().any(|local| dom.is(element, W, local));
        if !(is_text || is_bookmark || laid_out_char) { continue; }
        if owning_paragraph(dom, element) != Some(paragraph) || is_hidden(dom, element) { continue; }
        if is_text {
            let text = dom.text(element).unwrap_or_default();
            let compact: String = text.chars().filter(|&c| !py_is_space(c)).collect();
            offset += compact.chars().count();
            chunks.push_str(&compact);
            laid_out += text.chars().count();
        } else if !is_bookmark {
            laid_out += 1;
        } else {
            let name = dom.attr(element, Some(W), "name").unwrap_or_default();
            if name.starts_with(&naming.marker_prefix) && !name.starts_with(&naming.anchor_prefix) {
                markers.push((element, offset, laid_out));
            }
        }
    }
    let mut moved = Vec::new();
    let mut kept = Vec::new();
    for (start, marker_offset, laid_out_before) in markers {
        // Only a marker with nothing at all (not even a space) on one side is
        // harmless: next to a trailing space it changes how a line is centered.
        if laid_out_before == 0 || laid_out_before == laid_out {
            kept.push((dom.attr(start, Some(W), "name").unwrap_or_default().to_owned(), marker_offset));
            continue;
        }
        let id = dom.attr(start, Some(W), "id").map(str::to_owned);
        if let Some(end) = dom.next(start) {
            if dom.is(end, W, "bookmarkEnd") && dom.attr(end, Some(W), "id").map(str::to_owned) == id {
                dom.remove(end);
            }
        }
        let name = dom.attr(start, Some(W), "name").unwrap_or_default().to_owned();
        dom.remove(start);
        moved.push((name, marker_offset));
    }
    ParagraphMarkers { moved, kept, text: chunks }
}

/// Insert a point bookmark after the paragraph's properties.
fn insert_paragraph_anchor(dom: &mut Dom, paragraph: NodeId, name: &str, id: &str) {
    let prefix = dom.prefix_for(paragraph, W).unwrap_or(Some("w".into()));
    let w = |local: &str| Name { prefix: prefix.clone(), local: local.into(), ns: Some(W.into()) };
    let start = dom.new_element(w("bookmarkStart"), vec![Attr { name: w("id"), value: id.into() }, Attr { name: w("name"), value: name.into() }]);
    let end = dom.new_element(w("bookmarkEnd"), vec![Attr { name: w("id"), value: id.into() }]);
    let properties = dom.children(paragraph).into_iter().find(|&c| dom.is(c, W, "pPr"));
    let index = properties.and_then(|p| dom.index(paragraph, p)).map_or(0, |i| i + 1);
    dom.insert(paragraph, index, start);
    dom.insert(paragraph, index + 1, end);
}

/// Move every in-line marker of `docx` to a paragraph anchor, also placing
/// the `relative` markers the render did not keep.
///
/// Fails when `word/document.xml` is missing or is not namespace-well-formed
/// (a filter can leave run XML a strict parser rejects); the caller then has
/// [`anchor_inline_markers_or_strip`].
pub fn anchor_inline_markers(docx: &[u8], naming: &Naming, relative: &[RelativeMarker]) -> Result<Anchored, String> {
    let mut package = Package::from_bytes(docx)?;
    let xml = package.get_string(DOCUMENT).ok_or("word/document.xml is missing")?;
    if !xml.contains(&naming.marker_prefix) {
        return Ok(Anchored { content: docx.to_vec(), inline_markers: Vec::new() });
    }
    if !is_strictly_parseable(&xml) {
        return Err("document.xml is not namespace-well-formed".into());
    }
    let mut dom = Dom::parse(&xml)?;
    let mut inline_markers = Vec::new();
    let mut next_id = naming.first_anchor_id;
    let paragraphs: Vec<NodeId> = dom.all_elements().into_iter().filter(|&e| dom.is(e, W, "p")).collect();
    for paragraph in paragraphs {
        let ParagraphMarkers { mut moved, kept, text: paragraph_text } =
            move_inline_markers(&mut dom, paragraph, naming);
        for marker in relative {
            let anchor_at = moved.iter().chain(&kept)
                .find(|(name, _)| *name == marker.relative_to)
                .map(|&(_, offset)| offset);
            if let Some(offset) = anchor_at {
                moved.push((marker.name.clone(), offset.saturating_sub(marker.chars_before)));
            }
        }
        if moved.is_empty() { continue; }
        let anchor = format!("{}{next_id}", naming.anchor_prefix);
        insert_paragraph_anchor(&mut dom, paragraph, &anchor, &next_id.to_string());
        next_id += 1;
        for (name, offset) in moved {
            inline_markers.push((name, InlineMarker {
                paragraph_anchor: anchor.clone(), offset, paragraph_text: paragraph_text.clone(),
            }));
        }
    }
    if inline_markers.is_empty() {
        return Ok(Anchored { content: docx.to_vec(), inline_markers });
    }
    package.set(DOCUMENT, dom.serialize().into_bytes());
    Ok(Anchored { content: package.to_bytes()?, inline_markers })
}

/// Anchored markers, or a document with no markers at all when anchoring
/// fails: an in-line marker left behind would change the layout, so the
/// safe fallback is to drop the markers and keep the document faithful.
pub fn anchor_inline_markers_or_strip(docx: &[u8], naming: &Naming, relative: &[RelativeMarker]) -> Anchored {
    if let Ok(anchored) = anchor_inline_markers(docx, naming, relative) { return anchored; }
    match strip_markers(docx, naming) {
        Ok(content) => Anchored { content, inline_markers: Vec::new() },
        Err(_) => Anchored { content: docx.to_vec(), inline_markers: Vec::new() },
    }
}

/// Escape a prefix for use inside a regular expression.
fn escape(literal: &str) -> String {
    let mut escaped = String::with_capacity(literal.len());
    for c in literal.chars() {
        if !c.is_alphanumeric() && c != '_' { escaped.push('\\'); }
        escaped.push(c);
    }
    escaped
}

/// Remove every marker bookmark, leaving the document as it is rendered
/// without them. Works on the raw XML, so it also applies to a document a
/// strict parser rejects.
pub fn strip_markers(docx: &[u8], naming: &Naming) -> Result<Vec<u8>, String> {
    let mut package = Package::from_bytes(docx)?;
    let xml = package.get_string(DOCUMENT).ok_or("word/document.xml is missing")?;
    let prefix = escape(&naming.marker_prefix);
    let starts = fancy_regex::Regex::new(&format!(
        r#"<w:bookmarkStart\b[^>]*\bw:id="(\d+)"[^>]*\bw:name="{prefix}[^"]*"[^>]*/>"#)).expect("marker id pattern");
    let mut ids: Vec<String> = starts.captures_iter(&xml).flatten()
        .filter_map(|c| c.get(1).map(|m| m.as_str().to_owned())).collect();
    if ids.is_empty() { return Ok(docx.to_vec()); }
    ids.sort();
    ids.dedup();
    let marker = fancy_regex::Regex::new(&format!(
        r#"<w:bookmarkStart\b[^>]*\bw:name="{prefix}[^"]*"[^>]*/>"#)).expect("marker pattern");
    let xml = marker.replace_all(&xml, "").into_owned();
    let ends = fancy_regex::Regex::new(&format!(
        r#"<w:bookmarkEnd\b[^>]*\bw:id="({})"[^>]*/>"#, ids.join("|"))).expect("marker end pattern");
    let xml = ends.replace_all(&xml, "").into_owned();
    package.set(DOCUMENT, xml.into_bytes());
    package.to_bytes()
}

/// Whether a strict XML parser accepts `xml`: well-formed, and every element
/// and attribute prefix declared. lxml's default parser is this strict, and
/// a pipeline that feeds it must not hand it less.
pub fn is_strictly_parseable(xml: &str) -> bool {
    use quick_xml::events::Event;
    use quick_xml::name::ResolveResult;
    if !docxtplrs::recover::is_well_formed(xml) { return false; }
    let mut reader = quick_xml::NsReader::from_str(xml);
    loop {
        match reader.read_resolved_event() {
            Ok((resolved, Event::Start(element) | Event::Empty(element))) => {
                if matches!(resolved, ResolveResult::Unknown(_)) { return false; }
                for attribute in element.attributes() {
                    let Ok(attribute) = attribute else { return false };
                    if attribute.key.as_namespace_binding().is_some() { continue; }
                    let (resolved, _) = reader.resolve_attribute(attribute.key);
                    if matches!(resolved, ResolveResult::Unknown(_)) { return false; }
                }
            }
            Ok((_, Event::Eof)) => return true,
            Ok(_) => {}
            Err(_) => return false,
        }
    }
}
