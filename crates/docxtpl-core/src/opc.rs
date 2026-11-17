//! OPC writer compatibility with python-docx 1.2.0 (MIT; vendor/notices).
//! Rebuild the graph and index; retain opaque payloads and compressed media.
use docxtplrs::package::{Package, rels_path_for};
use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::sync::{Arc, Mutex, OnceLock};

const CT: &str = "http://schemas.openxmlformats.org/package/2006/content-types";
const REL: &str = "http://schemas.openxmlformats.org/package/2006/relationships";
const REL_TYPE: &str = "application/vnd.openxmlformats-package.relationships+xml";
const DECL: &str = "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n";

fn parse(source: &[u8]) -> Result<libxml::tree::Document, String> {
    let options = libxml::parser::ParserOptions { recover: false, no_net: true, no_blanks: true, ..Default::default() };
    libxml::parser::Parser::default().parse_string_with_options(source, options).map_err(|_| "invalid OPC XML".into())
}

/// Serialize `node` as lxml's `tostring(encoding="UTF-8")` text does.
///
/// libxml writes non-ASCII attribute characters as character references when
/// its document has no encoding (no prolog, a stripped prolog or a built
/// tree); lxml with an explicit UTF-8 encoding keeps them as raw UTF-8.
pub fn node_to_utf8_string(document: &libxml::tree::Document, node: &libxml::tree::Node) -> String {
    // SAFETY: the document pointer is live for `document`'s lifetime and is
    // freed by xmlFreeDoc, which releases `encoding` with the same allocator.
    unsafe {
        let doc = document.doc_ptr();
        if !doc.is_null() && (*doc).encoding.is_null() {
            (*doc).encoding = libxml::bindings::xmlStrdup(c"UTF-8".as_ptr().cast());
        }
    }
    document.node_to_string(node)
}

/// XmlPart.load removes blank structural nodes and later serializes UTF-8.
/// Input here is already decoded Unicode, so discard its byte-encoding prolog.
pub fn normalize_xml_part(source: &str) -> Result<String, String> {
    let source = source.strip_prefix('\u{feff}').unwrap_or(source);
    let source = if source.starts_with("<?xml ") {
        &source[source.find("?>").ok_or("unterminated XML declaration")? + 2..]
    } else { source };
    let document = parse(source.as_bytes())?;
    let root = document.get_root_element().ok_or("missing XML part root")?;
    Ok(node_to_utf8_string(&document, &root))
}

/// Normalize a rendered header/footer and retain a parser diagnostic for the
/// host binding. Parsing and diagnostic collection remain entirely in libxml.
pub fn normalize_rendered_header_footer(source: &str) -> Result<String, String> {
    // The host needs the part exactly as it arrived: the reference hands that
    // string to its own parser, and only that parser can produce the error log
    // the reference's exception carries.
    let as_received = source;
    let source = source.strip_prefix('\u{feff}').unwrap_or(source);
    let source = if source.starts_with("<?xml ") {
        &source[source.find("?>").ok_or("unterminated XML declaration")? + 2..]
    } else { source };
    let options = libxml::parser::ParserOptions { recover: false, no_net: true, no_blanks: true, ..Default::default() };
    let document = match libxml::parser::Parser::default().parse_string_with_diagnostics(source.as_bytes(), options) {
        Ok((document, _)) => document,
        Err(libxml::parser::XmlParseFailure::ParseFailed(errors)) => {
            let Some(error) = errors.first() else { return Err("invalid OPC XML".into()); };
            // Some libxml versions report NUL as truncated input. Convert only
            // when the first diagnostic points to the NUL itself, preserving
            // an earlier syntax error and its position.
            let at_nul = error.line.zip(error.col).and_then(|(line, column)| {
                let line = usize::try_from(line).ok()?.checked_sub(1)?;
                let column = usize::try_from(column).ok()?.checked_sub(1)?;
                source.split('\n').nth(line)?.chars().nth(column)
            }) == Some('\0');
            let message = if at_nul { Some("Invalid character: Char 0x0 out of allowed range\n") }
                else { error.message.as_deref().map(|message| message.trim_end_matches('\n')) };
            let diagnostic = serde_json::json!({"message": message, "code": if at_nul { 9 } else { error.code },
                "line": error.line, "column": error.col, "source": as_received});
            return Err(format!("DOCXTPL_HEADER_XML_ERROR:{diagnostic}"));
        }
        Err(_) => return Err("invalid OPC XML".into()),
    };
    let root = document.get_root_element().ok_or("missing XML part root")?;
    Ok(node_to_utf8_string(&document, &root))
}

/// Recover rendered XML that contains an illegal character. The text is
/// repaired first (`docxtplrs::recover`), so entity references and stray
/// `&`/`<` are kept where libxml2's recover mode would drop them (intentional
/// divergence from docxtpl); libxml2 then only serializes the result.
pub fn recover_rendered_xml(source: &str) -> Result<String, String> {
    let options = libxml::parser::ParserOptions { recover: true, no_net: true, ..Default::default() };
    let normalized = docxtplrs::recover::recover(source);
    let document = libxml::parser::Parser::default().parse_string_with_options(normalized.as_bytes(), options)
        .map_err(|_| "invalid rendered XML".to_owned())?;
    let root = document.get_root_element().ok_or("missing rendered XML root")?;
    Ok(node_to_utf8_string(&document, &root))
}

/// Diagnostic only: the linked libxml implementation is not yet equivalent.
#[cfg(test)]
fn recover_rendered_unicode_xml(codepoints: &[u32]) -> Result<String, String> {
    let mut bytes = vec![0xff, 0xfe];
    let mut index = 0;
    while index < codepoints.len() {
        let code = codepoints[index];
        if (0xd800..=0xdbff).contains(&code) {
            bytes.extend_from_slice(&(code as u16).to_le_bytes());
            if codepoints.get(index + 1).is_some_and(|next| (0xdc00..=0xdfff).contains(next)) {
                index += 1;
                bytes.extend_from_slice(&(codepoints[index] as u16).to_le_bytes());
            } else if index + 1 == codepoints.len() {
                bytes.extend_from_slice(&u16::from(b'A').to_le_bytes());
            }
        } else if (0xdc00..=0xdfff).contains(&code) {
            // Some libxml builds silently discard isolated low surrogates.
            // A malformed high surrogate makes recovery stop at this point.
            bytes.extend_from_slice(&0xd800u16.to_le_bytes());
            bytes.extend_from_slice(&u16::from(b'A').to_le_bytes());
        } else {
            let character = char::from_u32(code).ok_or("invalid Unicode code point")?;
            for unit in character.encode_utf16(&mut [0; 2]).iter() {
                bytes.extend_from_slice(&unit.to_le_bytes());
            }
        }
        index += 1;
    }
    let options = libxml::parser::ParserOptions { recover: true, no_net: true, ..Default::default() };
    let document = libxml::parser::Parser::default().parse_string_with_options(&bytes, options)
        .map_err(|_| "invalid rendered XML".to_owned())?;
    let root = document.get_root_element().ok_or("missing rendered XML root")?;
    Ok(document.node_to_string(&root))
}
fn root(document: &libxml::tree::Document, name: &str, uri: &str) -> Result<libxml::tree::Node, String> {
    let node = document.get_root_element().ok_or("missing OPC XML root")?;
    if node.get_name() != name || !node.get_namespace().is_some_and(|ns| ns.get_href() == uri) {
        return Err(format!("invalid OPC {name} root or namespace"));
    }
    Ok(node)
}
fn attribute(node: &libxml::tree::Node, name: &str) -> Result<String, String> {
    node.get_property(name).ok_or_else(|| format!("missing OPC {name} attribute"))
}
fn xml(document: &libxml::tree::Document, root: &libxml::tree::Node) -> Vec<u8> {
    format!("{DECL}{}", node_to_utf8_string(document, root)).into_bytes()
}

/// PackURI uses POSIX paths, without URL/percent decoding. Leading // survives.
fn absolute(source: &str, target: &str) -> String {
    let base = source.rsplit_once('/').map(|(base, _)| base).unwrap_or("");
    let combined = if target.starts_with('/') { target.to_owned() } else { format!("{base}/{target}") };
    let prefix = if combined.starts_with("//") && !combined.starts_with("///") { "//" } else { "/" };
    let mut segments = Vec::new();
    for segment in combined.split('/') {
        match segment { "" | "." => {}, ".." => { segments.pop(); }, _ => segments.push(segment) }
    }
    format!("{prefix}{}", segments.join("/"))
}
fn relative(source: &str, target: &str) -> String {
    let base = source.rsplit_once('/').map(|(base, _)| base).unwrap_or("");
    if base.is_empty() { return target[1..].to_owned(); }
    let from: Vec<_> = base.split('/').filter(|p| !p.is_empty()).collect();
    let to: Vec<_> = target.split('/').filter(|p| !p.is_empty()).collect();
    let common = from.iter().zip(&to).take_while(|(a,b)| a == b).count();
    let path = std::iter::repeat_n("..", from.len()-common).chain(to[common..].iter().copied()).collect::<Vec<_>>().join("/");
    if path.is_empty() { ".".to_owned() } else { path }
}
fn extension(path: &str) -> &str {
    let filename = path.rsplit('/').next().unwrap_or("");
    match filename.rsplit_once('.') { Some((stem, ext)) if stem.chars().any(|c| c != '.') => ext, _ => "" }
}

#[derive(Clone, Debug)]
struct Relationship { id: String, kind: String, target: String, external: bool }
fn relationships(package: &Package, source: &str) -> Result<Vec<Relationship>, String> {
    read_relationships(package, source, true)
}
fn read_relationships(package: &Package, source: &str, collapse_ids: bool) -> Result<Vec<Relationship>, String> {
    let Some(bytes) = package.get_arc(&rels_path_for(&source[1..])) else { return Ok(Vec::new()); };
    let document = parse(&bytes)?;
    let root = root(&document, "Relationships", REL)?;
    let mut result = Vec::new();
    let mut positions = HashMap::new();
    for node in root.get_child_elements() {
        if node.get_name() != "Relationship" || !node.get_namespace().is_some_and(|ns| ns.get_href() == REL) { continue; }
        let id = attribute(&node, "Id")?;
        let external = node.get_property("TargetMode").as_deref() == Some("External");
        let target = attribute(&node, "Target")?;
        let relationship = Relationship { id: id.clone(), kind: attribute(&node, "Type")?,
            target: if external { target } else { absolute(source, &target) }, external };
        // Last value wins, retaining the first position of the ID in the dict.
        if let Some(index) = positions.get(&id).filter(|_| collapse_ids) { result[*index] = relationship; }
        else { positions.insert(id, result.len()); result.push(relationship); }
    }
    Ok(result)
}

/// Traverse internal relationships once in the same depth-first order as OPC.
fn reachable_parts(package: &Package, roots: &[Relationship]) -> Result<Vec<(String, Vec<Relationship>)>, String> {
    walk_parts(package, roots, true)
}
fn walk_parts(package: &Package, roots: &[Relationship], collapse_ids: bool) -> Result<Vec<(String, Vec<Relationship>)>, String> {
    let mut stack: Vec<_> = roots.iter().rev().filter(|r| !r.external).map(|r| r.target.clone()).collect();
    let mut visited = HashSet::new();
    let mut parts = Vec::new();
    while let Some(name) = stack.pop() {
        if !visited.insert(name.clone()) { continue; }
        if !package.contains(&name[1..]) { return Err(format!("missing OPC target '{name}'")); }
        let rels = read_relationships(package, &name, collapse_ids)?;
        stack.extend(rels.iter().rev().filter(|r| !r.external).map(|r| r.target.clone()));
        parts.push((name, rels));
    }
    Ok(parts)
}

/// Last payload read while loading an OPC graph. The physical reader visits
/// serialized relationships before IDs are collapsed into the live dictionaries.
/// Archive order and orphan entries do not determine the exposed stream cursor.
pub fn last_loaded_part(package: &Package) -> Result<String, String> {
    let roots = read_relationships(package, "/", false)?;
    Ok(walk_parts(package, &roots, false)?.pop().map(|(name, _)| name[1..].to_owned())
        .unwrap_or_else(|| if package.contains("_rels/.rels") { "_rels/.rels" } else { "[Content_Types].xml" }.to_owned()))
}
fn write_relationships(source: &str, relationships: &[Relationship]) -> Result<Vec<u8>, String> {
    let document = parse(format!("<Relationships xmlns=\"{REL}\"/>").as_bytes())?;
    let mut root = root(&document, "Relationships", REL)?;
    let namespace = root.get_namespace().unwrap();
    for relationship in relationships {
        let mut node = root.new_child(Some(namespace.clone()), "Relationship").map_err(|e| e.to_string())?;
        node.set_property("Id", &relationship.id).map_err(|e| e.to_string())?;
        node.set_property("Type", &relationship.kind).map_err(|e| e.to_string())?;
        node.set_property("Target", &if relationship.external { relationship.target.clone() } else { relative(source, &relationship.target) }).map_err(|e| e.to_string())?;
        if relationship.external { node.set_property("TargetMode", "External").map_err(|e| e.to_string())?; }
    }
    Ok(xml(&document, &root))
}

/// python-docx's `.rels` XML for relationships given as (Id, Type, Target as
/// written, external).
pub fn relationships_xml(entries: &[(String, String, String, bool)]) -> Result<Vec<u8>, String> {
    let document = parse(format!("<Relationships xmlns=\"{REL}\"/>").as_bytes())?;
    let mut root = root(&document, "Relationships", REL)?;
    let namespace = root.get_namespace().unwrap();
    for (id, kind, target, external) in entries {
        let mut node = root.new_child(Some(namespace.clone()), "Relationship").map_err(|e| e.to_string())?;
        node.set_property("Id", id).map_err(|e| e.to_string())?;
        node.set_property("Type", kind).map_err(|e| e.to_string())?;
        node.set_property("Target", target).map_err(|e| e.to_string())?;
        if *external { node.set_property("TargetMode", "External").map_err(|e| e.to_string())?; }
    }
    Ok(xml(&document, &root))
}

/// A ZIP archive of `members` in order, with the Python OPC writer's
/// compression and metadata (as `to_bytes_opc`). A repeated name keeps the
/// last data at its first position.
pub fn opc_archive(members: Vec<(String, Vec<u8>)>) -> Result<Vec<u8>, String> {
    let mut package = Package::from_bytes(&docxtplrs::package::minimal_docx(""))?;
    package.entries.clear();
    package.index.clear();
    for (name, data) in members { package.set(&name, data); }
    package.to_bytes_opc()
}

pub struct Hyperlinks { source: Arc<[u8]>, relationships: Vec<Relationship> }
impl Hyperlinks {
    pub fn new(source: &[u8]) -> Result<Self, String> {
        let source: Arc<[u8]> = source.to_vec().into();
        let package = Package::from_bytes_arc(source.clone())?;
        Ok(Self { source, relationships: relationships(&package, "/word/document.xml")? })
    }
    pub fn relate(&mut self, target: &str) -> String {
        const HYPERLINK: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink";
        if let Some(existing) = self.relationships.iter().find(|rel| rel.external && rel.kind == HYPERLINK && rel.target == target) {
            return existing.id.clone();
        }
        let used: HashSet<_> = self.relationships.iter().map(|rel| rel.id.as_str()).collect();
        let id = (1..self.relationships.len()+2).map(|number| format!("rId{number}")).find(|id| !used.contains(id.as_str())).unwrap();
        self.relationships.push(Relationship { id: id.clone(), kind: HYPERLINK.into(), target: target.into(), external: true });
        id
    }
    pub fn to_bytes(&self) -> Result<Vec<u8>, String> {
        // Package's Rc DOM caches stay local to this call; only owned bytes
        // and relationship strings persist across Python thread boundaries.
        let mut package = Package::from_bytes_arc(self.source.clone())?;
        package.set("word/_rels/document.xml.rels", write_relationships("/word/document.xml", &self.relationships)?);
        package.to_bytes()
    }
}

#[derive(Default)]
struct ContentTypes { defaults: HashMap<String, String>, overrides: HashMap<String, String> }
impl ContentTypes {
    fn read(package: &Package) -> Result<Self, String> {
        let bytes = package.get_arc("[Content_Types].xml").ok_or("missing content types")?;
        let document = parse(&bytes)?;
        let root = root(&document, "Types", CT)?;
        let mut result = Self::default();
        for node in root.get_child_elements() {
            if !node.get_namespace().is_some_and(|ns| ns.get_href() == CT) { continue; }
            let name = node.get_name();
            let (map, key) = match name.as_str() {
                "Default" => (&mut result.defaults, "Extension"), "Override" => (&mut result.overrides, "PartName"), _ => continue,
            };
            map.insert(attribute(&node, key)?.to_lowercase(), attribute(&node, "ContentType")?);
        }
        Ok(result)
    }
    fn get(&self, name: &str) -> Result<&str, String> {
        self.overrides.get(&name.to_lowercase()).or_else(|| self.defaults.get(&extension(name).to_lowercase()))
            .map(String::as_str).ok_or_else(|| format!("no content type for partname '{name}'"))
    }
}
/// Exact reference table: an unfamiliar extension/type pair uses an override.
fn standard_default(ext: &str, kind: &str) -> bool {
    matches!((ext, kind),
        ("bin", "application/vnd.openxmlformats-officedocument.presentationml.printerSettings") |
        ("bin", "application/vnd.openxmlformats-officedocument.spreadsheetml.printerSettings") |
        ("bin", "application/vnd.openxmlformats-officedocument.wordprocessingml.printerSettings") |
        ("bmp", "image/bmp") | ("emf", "image/x-emf") | ("fntdata", "application/x-fontdata") |
        ("gif", "image/gif") | ("jpe" | "jpeg" | "jpg", "image/jpeg") | ("png", "image/png") |
        ("rels", REL_TYPE) | ("tif" | "tiff", "image/tiff") | ("wdp", "image/vnd.ms-photo") |
        ("wmf", "image/x-wmf") | ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") |
        ("xml", "application/xml"))
}
fn write_content_types(parts: &[(String, Vec<Relationship>)], types: &ContentTypes) -> Result<Vec<u8>, String> {
    let entries = parts.iter().map(|(name, _)| Ok((name.clone(), extension(name).to_owned(), types.get(name)?.to_owned())))
        .collect::<Result<Vec<_>, String>>()?;
    content_types_xml(&entries)
}

/// python-docx's `[Content_Types].xml` for parts given as (partname, its
/// extension, content type): a standard extension/type pair becomes a
/// Default (keyed by the lowercased extension), anything else an Override;
/// both sorted, after the `rels` and `xml` defaults.
pub fn content_types_xml(parts: &[(String, String, String)]) -> Result<Vec<u8>, String> {
    let mut defaults = BTreeMap::from([("rels".to_owned(), REL_TYPE.to_owned()), ("xml".to_owned(), "application/xml".to_owned())]);
    let mut overrides = BTreeMap::new();
    for (name, ext, kind) in parts {
        let ext = ext.to_lowercase();
        if standard_default(&ext, kind) { defaults.insert(ext, kind.clone()); } else { overrides.insert(name.clone(), kind.clone()); }
    }
    let document = parse(format!("<Types xmlns=\"{CT}\"/>").as_bytes())?;
    let mut root = root(&document, "Types", CT)?;
    let namespace = root.get_namespace().unwrap();
    for (tag, key, values) in [("Default", "Extension", defaults), ("Override", "PartName", overrides)] {
        for (name, kind) in values {
            let mut node = root.new_child(Some(namespace.clone()), tag).map_err(|e| e.to_string())?;
            node.set_property(key, &name).map_err(|e| e.to_string())?;
            node.set_property("ContentType", &kind).map_err(|e| e.to_string())?;
        }
    }
    Ok(xml(&document, &root))
}

/// WordprocessingML parts registered as XmlPart subclasses by python-docx
/// 1.2.0 are parsed on load and serialized again on save. Core properties
/// use their own renderer, which preserves explicitly empty text nodes.
fn is_known_xml_part(kind: &str) -> bool {
    matches!(kind,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml" |
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml" |
        "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml" |
        "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml" |
        "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml" |
        "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml" |
        "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml")
}

// Cache only immutable bytes, never libxml nodes or package graphs. Bound both
// memory and entry count; hashing and comparing the full source prevents an
// unrelated document from receiving another document's XML.
const XML_CACHE_BYTES: usize = 16 * 1024 * 1024;
const XML_CACHE_ENTRIES: usize = 32;
#[derive(Default)]
struct XmlCache {
    entries: HashMap<Arc<[u8]>, (Arc<[u8]>, usize)>,
    order: VecDeque<Arc<[u8]>>,
    bytes: usize,
}
static XML_CACHE: OnceLock<Mutex<XmlCache>> = OnceLock::new();

fn normalized_part(source: &Arc<[u8]>) -> Result<Arc<[u8]>, String> {
    let cache = XML_CACHE.get_or_init(|| Mutex::new(XmlCache::default()));
    if let Some((result, _)) = cache.lock().unwrap_or_else(|e| e.into_inner()).entries.get(source.as_ref()) {
        return Ok(result.clone());
    }
    // Parsing and serialization must happen outside the cache lock.
    let document = parse(source)?;
    let element = document.get_root_element().ok_or("missing XML part root")?;
    let serialized = xml(&document, &element);
    let unchanged = serialized.as_slice() == source.as_ref();
    let result: Arc<[u8]> = if unchanged { source.clone() } else { serialized.into() };
    let size = source.len().saturating_add(if unchanged { 0 } else { result.len() });
    if size <= XML_CACHE_BYTES {
        let mut guard = cache.lock().unwrap_or_else(|e| e.into_inner());
        if let Some((existing, _)) = guard.entries.get(source.as_ref()) {
            return Ok(existing.clone());
        }
        while guard.entries.len() >= XML_CACHE_ENTRIES || guard.bytes + size > XML_CACHE_BYTES {
            let Some(oldest) = guard.order.pop_front() else { break; };
            if let Some((_, old_size)) = guard.entries.remove(oldest.as_ref()) {
                guard.bytes -= old_size;
            }
        }
        guard.bytes += size;
        guard.order.push_back(source.clone());
        guard.entries.insert(source.clone(), (result.clone(), size));
    }
    Ok(result)
}

/// python-docx enumerates package parts for each section, and docxtpl selects
/// footnotes by content type rather than relationship type.
pub fn footnote_parts(package: &Package, rendered_body: &str) -> Result<(Vec<String>, usize), String> {
    const W: &str = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
    const FOOTNOTES: &str = "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml";
    let types = ContentTypes::read(package)?;
    if !types.defaults.values().chain(types.overrides.values()).any(|kind| kind == FOOTNOTES) {
        return Ok((Vec::new(), 0));
    }
    let source = rendered_body.strip_prefix('\u{feff}').unwrap_or(rendered_body);
    let source = if source.starts_with("<?xml ") {
        &source[source.find("?>").ok_or("unterminated XML declaration")? + 2..]
    } else { source };
    let options = libxml::parser::ParserOptions { recover: true, no_net: true, ..Default::default() };
    let document = libxml::parser::Parser::default().parse_string_with_options(source.as_bytes(), options)
        .map_err(|_| "invalid rendered document XML")?;
    let root = document.get_root_element().ok_or("missing rendered document root")?;
    let is_word = |node: &libxml::tree::Node, name: &str| {
        node.get_name() == name && node.get_namespace().is_some_and(|ns| ns.get_href() == W)
    };
    let mut sections = 0usize;
    for body in root.get_child_elements().into_iter().filter(|n| is_word(n, "body")) {
        for child in body.get_child_elements() {
            if is_word(&child, "sectPr") { sections += 1; }
            if is_word(&child, "p") {
                for properties in child.get_child_elements().into_iter().filter(|n| is_word(n, "pPr")) {
                    sections += properties.get_child_elements().into_iter().filter(|n| is_word(n, "sectPr")).count();
                }
            }
        }
    }
    if sections == 0 { return Ok((Vec::new(), 0)); }
    let roots = relationships(package, "/")?;
    let mut notes = Vec::new();
    for (name, _) in reachable_parts(package, &roots)? {
        if types.get(&name)? == FOOTNOTES { notes.push(name[1..].to_owned()); }
    }
    Ok((notes, sections))
}

pub fn serialize(package: &Package) -> Result<Vec<u8>, String> {
    serialize_impl(package, true, false, None, false)
}

/// Keep XML already serialized by an exposed document model byte-for-byte.
pub fn serialize_model(package: &Package) -> Result<Vec<u8>, String> {
    serialize_impl(package, false, false, None, false)
}

/// Loading and saving a new document parses core properties as well. Unlike
/// render, this phase does not assign their values or recreate empty text nodes.
pub fn serialize_loaded(package: &Package) -> Result<Vec<u8>, String> {
    serialize_impl(package, true, true, None, false)
}

/// Save parts already loaded and rendered without dropping text nodes created
/// by the renderer. Other known XML parts retain the ordinary load policy.
pub fn serialize_rendered(package: &Package, rendered_parts: &HashSet<String>) -> Result<Vec<u8>, String> {
    serialize_impl(package, true, false, Some(rendered_parts), false)
}

/// Preserve a body already serialized by a host XML provider that retained
/// non-XML characters during recovery. Other package parts are normalized.
pub fn serialize_rendered_with_raw_body(package: &Package, rendered_parts: &HashSet<String>) -> Result<Vec<u8>, String> {
    serialize_impl(package, true, false, Some(rendered_parts), true)
}

fn serialize_impl(package: &Package, normalize_known_parts: bool, normalize_core: bool,
    preserve_rendered_parts: Option<&HashSet<String>>, preserve_raw_body: bool) -> Result<Vec<u8>, String> {
    let types = ContentTypes::read(package)?;
    let roots = relationships(package, "/")?;
    let parts = reachable_parts(package, &roots)?;
    // A write-only clone: no mutation of the live core or its relationship
    // caches, no inflation of unchanged compressed media during graph traversal.
    let mut output = package.clone();
    output.set("[Content_Types].xml", write_content_types(&parts, &types)?);
    output.set("_rels/.rels", write_relationships("/", &roots)?);
    let mut order = vec!["[Content_Types].xml".to_owned(), "_rels/.rels".to_owned()];
    for (name, rels) in parts {
        let kind = types.get(&name)?;
        if normalize_known_parts && !(preserve_raw_body && name == "/word/document.xml") && (is_known_xml_part(kind)
            || (normalize_core && kind == "application/vnd.openxmlformats-package.core-properties+xml")) {
            let bytes = package.get_arc(&name[1..]).ok_or("missing XML part")?;
            let preserve_blank_nodes = preserve_rendered_parts.is_some_and(|parts| parts.contains(&name[1..])) && matches!(kind,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml" |
                "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml" |
                "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml");
            let normalized = if preserve_blank_nodes {
                let options = libxml::parser::ParserOptions { recover: false, no_net: true,
                    no_blanks: false, ..Default::default() };
                let document = libxml::parser::Parser::default().parse_string_with_options(&*bytes, options)
                    .map_err(|_| "invalid rendered OPC XML")?;
                let element = document.get_root_element().ok_or("missing rendered XML part root")?;
                Arc::from(xml(&document, &element))
            } else { normalized_part(&bytes)? };
            if normalized.as_ref() != bytes.as_ref() {
                output.set(&name[1..], normalized.to_vec());
            }
        }
        order.push(name[1..].to_owned());
        if !rels.is_empty() {
            let path = rels_path_for(&name[1..]);
            output.set(&path, write_relationships(&name, &rels)?);
            order.push(path);
        }
    }
    let positions: HashMap<_, _> = order.into_iter().enumerate().map(|(i, name)| (name, i)).collect();
    output.entries.retain(|entry| positions.contains_key(&entry.name));
    output.entries.sort_by_key(|entry| positions[&entry.name]);
    output.index = output.entries.iter().enumerate().map(|(i,e)| (e.name.clone(), i)).collect();
    output.to_bytes_opc()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn non_ascii_attributes_stay_utf8_like_lxml() {
        let rels = write_relationships("/word/document.xml", &[Relationship { id: "rId1".into(),
            kind: "k".into(), target: "https://e.test/façade".into(), external: true }]).unwrap();
        assert!(String::from_utf8(rels).unwrap().contains("Target=\"https://e.test/façade\""));
        assert_eq!(normalize_xml_part("<?xml version=\"1.0\" encoding=\"UTF-8\"?><a b=\"façade\">ç</a>").unwrap(),
            "<a b=\"façade\">ç</a>");
        assert_eq!(normalize_rendered_header_footer("<a b=\"ç\"/>").unwrap(), "<a b=\"ç\"/>");
        assert_eq!(recover_rendered_xml("<a b=\"ç\">\u{1}</a>").unwrap(), "<a b=\"ç\"/>");
        // libxml2 alone: "<a><t>x  y</t></a>"
        assert_eq!(recover_rendered_xml("<a><t>x\u{1} &amp; y</t></a>").unwrap(), "<a><t>x &amp; y</t></a>");
        let source: Arc<[u8]> = Arc::from("<a b=\"ç\"/>".as_bytes());
        assert!(String::from_utf8(normalized_part(&source).unwrap().to_vec()).unwrap().ends_with("<a b=\"ç\"/>"));
        // An explicit prolog encoding is kept and still serializes raw UTF-8.
        let source: Arc<[u8]> = Arc::from(&b"<?xml version=\"1.0\" encoding=\"ISO-8859-1\"?><a b=\"\xe7\"/>"[..]);
        assert!(String::from_utf8(normalized_part(&source).unwrap().to_vec()).unwrap().ends_with("<a b=\"ç\"/>"));
    }


    #[test]
    fn header_diagnostic_carries_the_part_as_received() {
        // The host reparses this string with the reference's own parser to
        // obtain the error log of the exception, so the diagnostic has to
        // carry the part exactly as it arrived - prolog and BOM included.
        let source = "\u{feff}<?xml version=\"1.0\" encoding=\"UTF-8\"?><a><t>\u{1}</t></a>";
        let message = normalize_rendered_header_footer(source).unwrap_err();
        let diagnostic = message.strip_prefix("DOCXTPL_HEADER_XML_ERROR:").expect("header diagnostic");
        let diagnostic: serde_json::Value = serde_json::from_str(diagnostic).unwrap();
        assert_eq!(diagnostic["source"].as_str(), Some(source));
        assert!(diagnostic["line"].as_i64().is_some() && diagnostic["column"].as_i64().is_some());
    }

    #[test]
    fn utf16_recovery_pins_reference_and_system_libxml_cases() {
        let cases: [(&str, &str, &[u32], &str, &str); 10] = [
            ("text-high", "<r><t>A", &[0xd800], "B</t><u>end</u></r>", "<r><t>A</t></r>"),
            ("text-low", "<r><t>A", &[0xdfff], "B</t><u>end</u></r>", "<r><t>A</t></r>"),
            ("entity", "<r><t>A&amp;", &[0xd800], "B</t></r>", "<r><t>A</t></r>"),
            ("attribute", "<r a=\"a", &[0xd800], "b\"><t>end</t></r>", "<r/>"),
            ("comment", "<r><!--a", &[0xd800], "b--><t>end</t></r>", "<r/>"),
            ("astral", "<r><t>A😀", &[0xd800], "B</t></r>", "<r><t>A😀</t></r>"),
            ("surrogate-pair-codepoints", "<r><t>A", &[0xd800, 0xdc00], "B</t></r>", "<r><t>A𐀀B</t></r>"),
            ("two-lows", "<r><t>A", &[0xdfff, 0xdc00], "B</t></r>", "<r><t>A</t></r>"),
            ("before-entity", "<r><t>A", &[0xdfff], "&amp;B</t></r>", "<r><t>A</t></r>"),
            ("doctype", "<!DOCTYPE r><r><t>A", &[0xd800], "B</t></r>", "<r><t>A</t></r>"),
        ];
        // Older libxml2 is checked against the output measured with 2.9.13;
        // 2.14+ is checked against the newer library bundled by lxml 6.
        // Pin both expectations rather than skipping assertions or claiming
        // that the older library matches lxml 6. Other versions still need
        // independent validation of these version-specific expectations.
        let mut mismatches = Vec::new();
        for (name, before, surrogate, after, expected) in cases {
            let expected = match (libxml::bindings::LIBXML_VERSION, name) {
                (version, "entity") if version < 21400 => "<r><t>A&amp;</t></r>",
                (version, "attribute") if version < 21400 => "<r a=\"a\"/>",
                _ => expected,
            };
            let codepoints = before.chars().map(u32::from).chain(surrogate.iter().copied())
                .chain(after.chars().map(u32::from)).collect::<Vec<_>>();
            let actual = recover_rendered_unicode_xml(&codepoints).unwrap();
            if actual != expected { mismatches.push((name, actual, expected)); }
        }
        assert!(mismatches.is_empty(), "mismatches: {mismatches:?}");
    }

    fn minimal() -> Package {
        let mut zip = [0u8; 22]; zip[..4].copy_from_slice(b"PK\x05\x06");
        let mut package = Package::from_bytes(&zip).unwrap();
        package.set("word/document.xml", b"<document/>".to_vec());
        package.set("_rels/.rels", format!("<Relationships xmlns='{REL}'><Relationship Id='r' Type='main' Target='word/document.xml'/></Relationships>").into_bytes());
        package
    }
    #[test]
    fn matches_posix_part_uri_operations() {
        for (source, target, expected) in [("/word/document.xml", "../custom/a.bin", "/custom/a.bin"),
            ("/", "/word/a/../document.xml", "/word/document.xml"), ("/word/document.xml", "a%20b.xml", "/word/a%20b.xml"),
            ("/", "../../a", "/a"), ("/", "//a/b", "//a/b")] { assert_eq!(absolute(source, target), expected); }
        assert_eq!(relative("/word/document.xml", "/custom/a.bin"), "../custom/a.bin");
        assert_eq!(relative("/", "/word/document.xml"), "word/document.xml");
        assert_eq!(extension("/word/.hidden"), ""); assert_eq!(extension("/word/.hidden.PNG"), "PNG");
    }
    #[test]
    fn load_cursor_follows_serialized_relationships_before_id_collapse() {
        let mut package = minimal();
        package.set("second.bin", vec![2]); package.set("third.bin", vec![3]);
        package.set("orphan.bin", vec![4]);
        package.set("_rels/.rels", format!("<Relationships xmlns='{REL}'>\
            <Relationship Id='a' Type='main' Target='word/document.xml'/>\
            <Relationship Id='b' Type='opaque' Target='second.bin'/>\
            <Relationship Id='a' Type='opaque' Target='third.bin'/></Relationships>").into_bytes());
        assert_eq!(last_loaded_part(&package).unwrap(), "third.bin");
        let live_roots = relationships(&package, "/").unwrap();
        assert_eq!(reachable_parts(&package, &live_roots).unwrap().last().unwrap().0, "/second.bin");
    }
    #[test]
    fn cached_normalization_keeps_distinct_document_contents() {
        let a: Arc<[u8]> = Arc::from(&b"<?xml version='1.0'?><root><value>A</value></root>"[..]);
        let b: Arc<[u8]> = Arc::from(&b"<?xml version='1.0'?><root><value>B</value></root>"[..]);
        let first = normalized_part(&a).unwrap();
        let second = normalized_part(&b).unwrap();
        assert!(std::str::from_utf8(&first).unwrap().contains(">A<"));
        assert!(std::str::from_utf8(&second).unwrap().contains(">B<"));
        assert_eq!(normalized_part(&a).unwrap().as_ref(), first.as_ref());
    }
    #[test]
    fn graph_cycle_orphan_index_and_binary_preservation() {
        let mut package = minimal();
        package.set("orphan.bin", vec![1,2,3]); package.set("word/media/blob.DAT", vec![0,255,128,1]);
        package.set("[Content_Types].xml", format!("<Types xmlns='{CT}'><Default Extension='xml' ContentType='application/xml'/><Default Extension='unused' ContentType='unused'/><Override PartName='/WORD/MEDIA/BLOB.dat' ContentType='application/x-opaque'/></Types>").into_bytes());
        package.set("word/_rels/document.xml.rels", format!("<Relationships xmlns='{REL}'><Relationship Id='x' Type='opaque' Target='media/blob.DAT'/><Relationship Id='cycle' Type='self' Target='/word/./document.xml' TargetMode='Internal'/></Relationships>").into_bytes());
        let result = Package::from_bytes(&serialize(&package).unwrap()).unwrap();
        assert!(!result.contains("orphan.bin")); assert!(package.contains("orphan.bin"));
        assert_eq!(result.get("word/media/blob.DAT"), Some(&[0,255,128,1][..]));
        let types = result.get_string("[Content_Types].xml").unwrap(); assert!(!types.contains("unused"));
        assert!(types.contains("Extension=\"rels\"")); assert!(types.contains("PartName=\"/word/media/blob.DAT\""));
        let rels = result.get_string("word/_rels/document.xml.rels").unwrap();
        assert!(rels.contains("Target=\"document.xml\"")); assert!(!rels.contains("TargetMode"));
    }
    #[test]
    fn rejects_missing_target_and_invalid_index() {
        let mut package = minimal();
        package.set("[Content_Types].xml", format!("<Types xmlns='{CT}'><Default Extension='xml' ContentType='application/xml'/></Types>").into_bytes());
        package.set("_rels/.rels", format!("<Relationships xmlns='{REL}'><Relationship Id='r' Type='missing' Target='missing.xml'/></Relationships>").into_bytes());
        assert!(serialize(&package).unwrap_err().contains("missing OPC target"));
        package.set("[Content_Types].xml", b"<Types xmlns='wrong'/>".to_vec()); assert!(serialize(&package).is_err());
    }
}
