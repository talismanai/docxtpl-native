//! Dissolving DOCPROPERTY fields that reference custom document properties
//! (docProps/custom.xml) into literal values — the Composer's
//! `remove_property_fields` option (docxcompose
//! `CustomProperties.dissolve_fields` parity): fields naming a property that
//! exists in custom.xml are replaced by the property's value; fields naming
//! unknown properties (or all fields when there is no custom.xml) are left
//! untouched.

use crate::package::{Package, Rels};
use crate::template::DOCUMENT_PART;
use crate::xmldom::{Document, Element, Node};

const CUSTOM_PROPS_PART: &str = "docProps/custom.xml";
const CUSTOM_PROPS_CONTENT_TYPE: &str =
    "application/vnd.openxmlformats-officedocument.custom-properties+xml";
const CUSTOM_PROPS_REL_TYPE: &str =
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties";
const CUSTOM_PROPS_FMTID: &str = "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}";
const DEFAULT_CUSTOM_XML: &str = "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n<Properties xmlns=\"http://schemas.openxmlformats.org/officeDocument/2006/custom-properties\" xmlns:vt=\"http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes\"></Properties>";

/// A typed custom document property value (docProps/custom.xml).
#[derive(Debug, Clone, PartialEq)]
pub enum CustomValue {
    Str(String),
    Bool(bool),
    Int(i32),
    Float(f64),
    /// W3CDTF timestamp, e.g. "2024-01-02T03:04:05Z" (vt:filetime)
    DateTime(String),
}

impl CustomValue {
    /// The vt:* tag carrying this value in custom.xml.
    fn tag(&self) -> &'static str {
        match self {
            CustomValue::Str(_) => "vt:lpwstr",
            CustomValue::Bool(_) => "vt:bool",
            CustomValue::Int(_) => "vt:i4",
            CustomValue::Float(_) => "vt:r8",
            CustomValue::DateTime(_) => "vt:filetime",
        }
    }

    fn text(&self) -> String {
        match self {
            CustomValue::Str(s) => s.clone(),
            CustomValue::Bool(b) => if *b { "true" } else { "false" }.to_string(),
            CustomValue::Int(i) => i.to_string(),
            CustomValue::Float(f) => f.to_string(),
            CustomValue::DateTime(s) => s.clone(),
        }
    }
}

/// Create docProps/custom.xml (plus its [Content_Types].xml override and
/// _rels/.rels relationship) when the package does not have it yet.
pub fn ensure_custom_part(pkg: &mut Package) {
    if !pkg.contains(CUSTOM_PROPS_PART) {
        pkg.set(CUSTOM_PROPS_PART, DEFAULT_CUSTOM_XML.as_bytes().to_vec());
    }
    pkg.ensure_content_type_override("docProps/custom.xml", CUSTOM_PROPS_CONTENT_TYPE);
    let rels_path = "_rels/.rels";
    let mut rels = pkg
        .get_string(rels_path)
        .map(|x| Rels::from_xml(&x))
        .unwrap_or_default();
    rels.add(CUSTOM_PROPS_REL_TYPE, "docProps/custom.xml", false);
    pkg.set(rels_path, rels.to_xml().into_bytes());
}

/// Parse docProps/custom.xml into typed (name, value) pairs (docxcompose
/// CustomProperties). Unknown value types fall back to their text content.
pub fn parse_custom_properties(xml: &str) -> Vec<(String, CustomValue)> {
    let Ok(dom) = Document::parse(xml) else {
        return Vec::new();
    };
    let mut els: Vec<&Element> = Vec::new();
    dom.root.iter_descendants("property", &mut els);
    let mut props = Vec::new();
    for p in els {
        let Some(name) = p.get_attr("name") else {
            continue;
        };
        let Some(value_el) = p.children.iter().find_map(|c| match c {
            Node::Elem(e) => Some(e),
            Node::Text(_) => None,
        }) else {
            continue;
        };
        let text = value_el.text_content();
        let value = match value_el.name.as_str() {
            "vt:bool" => CustomValue::Bool(text.trim().eq_ignore_ascii_case("true")),
            "vt:i4" | "vt:int" | "vt:i2" => {
                CustomValue::Int(text.trim().parse().unwrap_or(0))
            }
            "vt:r8" => CustomValue::Float(text.trim().parse().unwrap_or(0.0)),
            "vt:filetime" => CustomValue::DateTime(text.trim().to_string()),
            _ => CustomValue::Str(text),
        };
        props.push((name.to_string(), value));
    }
    props
}

/// All custom document properties of the package (empty when there is no
/// docProps/custom.xml part).
pub fn read_properties(pkg: &Package) -> Vec<(String, CustomValue)> {
    match pkg.get_string(CUSTOM_PROPS_PART) {
        Some(xml) => parse_custom_properties(&xml),
        None => Vec::new(),
    }
}

/// Set (insert or overwrite) one custom document property, creating
/// docProps/custom.xml when missing. pids are assigned incrementally.
pub fn set_property(pkg: &mut Package, name: &str, value: &CustomValue) -> Result<(), String> {
    ensure_custom_part(pkg);
    let xml = pkg
        .get_string(CUSTOM_PROPS_PART)
        .ok_or_else(|| "docProps/custom.xml not found".to_string())?;
    let mut dom = Document::parse(&xml)?;
    let mut max_pid = 1u32;
    let mut existing = None;
    for (i, c) in dom.root.children.iter().enumerate() {
        if let Node::Elem(e) = c {
            if e.name != "property" {
                continue;
            }
            if let Some(pid) = e.get_attr("pid").and_then(|p| p.parse::<u32>().ok()) {
                max_pid = max_pid.max(pid);
            }
            if e.get_attr("name") == Some(name) {
                existing = Some(i);
            }
        }
    }
    let mut value_el = Element::new(value.tag());
    value_el.children.push(Node::Text(value.text()));
    match existing {
        Some(i) => {
            if let Node::Elem(e) = &mut dom.root.children[i] {
                e.children = vec![Node::Elem(value_el)];
            }
        }
        None => {
            let mut p = Element::new("property");
            p.set_attr("fmtid", CUSTOM_PROPS_FMTID);
            p.set_attr("pid", &(max_pid + 1).to_string());
            p.set_attr("name", name);
            p.children.push(Node::Elem(value_el));
            dom.root.children.push(Node::Elem(p));
        }
    }
    pkg.set(CUSTOM_PROPS_PART, dom.serialize().into_bytes());
    Ok(())
}

/// Delete one custom document property; false when it did not exist.
pub fn delete_property(pkg: &mut Package, name: &str) -> Result<bool, String> {
    let Some(xml) = pkg.get_string(CUSTOM_PROPS_PART) else {
        return Ok(false);
    };
    let mut dom = Document::parse(&xml)?;
    let before = dom.root.children.len();
    dom.root.children.retain(|c| !matches!(c, Node::Elem(e) if e.name == "property" && e.get_attr("name") == Some(name)));
    if dom.root.children.len() == before {
        return Ok(false);
    }
    pkg.set(CUSTOM_PROPS_PART, dom.serialize().into_bytes());
    Ok(true)
}

/// Replace DOCPROPERTY fields referencing custom document properties by
/// their literal values in `word/document.xml` and all
/// `word/header*.xml`/`word/footer*.xml` parts of the given docx. Returns
/// the (possibly unchanged) docx bytes.
pub fn dissolve_custom_property_fields(docx_bytes: &[u8]) -> Result<Vec<u8>, String> {
    let mut pkg = Package::from_bytes(docx_bytes)?;
    let Some(custom_xml) = pkg.get_string(CUSTOM_PROPS_PART) else {
        return Ok(docx_bytes.to_vec());
    };
    let props = custom_properties(&custom_xml);
    if props.is_empty() {
        return Ok(docx_bytes.to_vec());
    }
    let mut parts: Vec<String> = Vec::new();
    if pkg.contains(DOCUMENT_PART) {
        parts.push(DOCUMENT_PART.to_string());
    }
    for e in &pkg.entries {
        if (e.name.starts_with("word/header") || e.name.starts_with("word/footer"))
            && e.name.ends_with(".xml")
        {
            parts.push(e.name.clone());
        }
    }
    for part in parts {
        let Some(xml) = pkg.get_string(&part) else {
            continue;
        };
        if !xml.contains("DOCPROPERTY") {
            continue;
        }
        let enc = pkg.encoding_of(&part);
        if let Some(new_xml) = dissolve_in_part(&xml, &props) {
            pkg.set(&part, crate::package::encode_part_owned(new_xml, &enc));
        }
    }
    pkg.to_bytes()
}

/// Parse docProps/custom.xml into (name, display value) pairs. Values live in
/// a typed child element (`vt:lpwstr`, `vt:i4`, ...); booleans render as
/// "Y"/"N" (docxcompose FieldBase._format_value).
fn custom_properties(xml: &str) -> Vec<(String, String)> {
    let Ok(dom) = Document::parse(xml) else {
        return Vec::new();
    };
    let mut els: Vec<&Element> = Vec::new();
    dom.root.iter_descendants("property", &mut els);
    let mut props = Vec::new();
    for p in els {
        let Some(name) = p.get_attr("name") else {
            continue;
        };
        let Some(value_el) = p.children.iter().find_map(|c| match c {
            Node::Elem(e) => Some(e),
            Node::Text(_) => None,
        }) else {
            continue;
        };
        let text = value_el.text_content();
        let value = if value_el.name == "vt:bool" {
            if text.trim().eq_ignore_ascii_case("true") {
                "Y".to_string()
            } else {
                "N".to_string()
            }
        } else {
            text
        };
        props.push((name.to_string(), value));
    }
    props
}

fn prop_value<'a>(props: &'a [(String, String)], name: &str) -> Option<&'a str> {
    props
        .iter()
        .find(|(n, _)| n.eq_ignore_ascii_case(name))
        .map(|(_, v)| v.as_str())
}

/// Parse the property name out of a DOCPROPERTY instruction
/// (` DOCPROPERTY "name" \* MERGEFORMAT ` and quoting variants). More lenient
/// than docxcompose's regex (which requires a trailing `\* MERGEFORMAT`).
fn docproperty_name(instr: &str) -> Option<String> {
    let idx = instr.find("DOCPROPERTY")?;
    let rest = instr[idx + "DOCPROPERTY".len()..].trim_start();
    let name = if let Some(q) = rest.strip_prefix('"') {
        let end = q.find('"')?;
        &q[..end]
    } else {
        rest.split_whitespace().next()?
    };
    if name.is_empty() {
        None
    } else {
        Some(name.to_string())
    }
}

/// Dissolve the known DOCPROPERTY fields of one part; None when nothing
/// changed (or the part does not parse).
fn dissolve_in_part(xml: &str, props: &[(String, String)]) -> Option<String> {
    let mut dom = Document::parse(xml).ok()?;
    let mut changed = false;
    dissolve_in_element(&mut dom.root, props, &mut changed);
    if changed {
        Some(dom.serialize())
    } else {
        None
    }
}

fn dissolve_in_element(el: &mut Element, props: &[(String, String)], changed: &mut bool) {
    // simple fields: <w:fldSimple w:instr=" DOCPROPERTY ...">runs</w:fldSimple>
    for i in 0..el.children.len() {
        let replacement = match &el.children[i] {
            Node::Elem(e) if e.name == "w:fldSimple" => e
                .get_attr("w:instr")
                .filter(|instr| instr.contains("DOCPROPERTY"))
                .and_then(docproperty_name)
                .and_then(|name| prop_value(props, &name))
                .map(|value| simple_field_replacement(e, value)),
            _ => None,
        };
        if let Some(node) = replacement {
            el.children[i] = node;
            *changed = true;
        }
    }
    // complex fields: begin -> instrText -> separate -> value runs -> end
    dissolve_complex_fields(el, props, changed);
    for c in el.children.iter_mut() {
        if let Node::Elem(e) = c {
            dissolve_in_element(e, props, changed);
        }
    }
}

/// docxcompose SimpleField.replace_field_with_value: the field wrapper is
/// dropped, its first run kept; here the run's text is also set to the
/// current property value (a stale cached value is not kept).
fn simple_field_replacement(fld: &Element, value: &str) -> Node {
    let mut run = match fld.children.iter().find_map(|c| match c {
        Node::Elem(e) if e.name == "w:r" => Some(e.clone()),
        _ => None,
    }) {
        Some(r) => r,
        None => Element::new("w:r"),
    };
    set_run_text(&mut run, value);
    Node::Elem(run)
}

/// Set the run's first w:t to `value`, clear any further w:t (a stale cached
/// value split over several text nodes must not leak through), or append a
/// w:t when the run has none.
fn set_run_text(run: &mut Element, value: &str) {
    let mut done = false;
    set_first_wt_and_clear_rest(run, value, &mut done);
    if !done {
        let mut t = Element::new("w:t");
        t.set_attr("xml:space", "preserve");
        t.children.push(Node::Text(value.to_string()));
        run.children.push(Node::Elem(t));
    }
}

fn set_first_wt_and_clear_rest(el: &mut Element, value: &str, done: &mut bool) {
    for c in el.children.iter_mut() {
        if let Node::Elem(e) = c {
            if e.name == "w:t" {
                if *done {
                    e.children.clear();
                } else {
                    e.children = vec![Node::Text(value.to_string())];
                    e.set_attr("xml:space", "preserve");
                    *done = true;
                }
            } else {
                set_first_wt_and_clear_rest(e, value, done);
            }
        }
    }
}

fn run_fld_char_type(run: &Element) -> Option<String> {
    let mut out: Vec<&Element> = Vec::new();
    run.iter_descendants("w:fldChar", &mut out);
    out.first()
        .and_then(|e| e.get_attr("w:fldCharType"))
        .map(|s| s.to_string())
}

fn run_instr_text(run: &Element) -> String {
    let mut out: Vec<&Element> = Vec::new();
    run.iter_descendants("w:instrText", &mut out);
    out.iter().map(|e| e.text_content()).collect()
}

fn has_wt(el: &Element) -> bool {
    let mut out: Vec<&Element> = Vec::new();
    el.iter_descendants("w:t", &mut out);
    !out.is_empty()
}

/// Clone a field run without its fldChar (keeps the run properties as a
/// template for the value run, like docxcompose's ComplexField.update).
fn value_run_template(run: &Element) -> Element {
    let mut r = run.clone();
    r.children
        .retain(|c| !matches!(c, Node::Elem(e) if e.name == "w:fldChar" || e.name == "w:instrText"));
    r
}

/// Dissolve complex DOCPROPERTY fields among the direct children of `el`
/// (fields never span paragraphs in practice: begin/instr/separate/end runs
/// are siblings inside one w:p).
fn dissolve_complex_fields(el: &mut Element, props: &[(String, String)], changed: &mut bool) {
    let mut scan_from = 0usize;
    while scan_from < el.children.len() {
        // next field-begin run at/after scan_from
        let mut begin = None;
        for i in scan_from..el.children.len() {
            if let Node::Elem(r) = &el.children[i] {
                if r.name == "w:r" && run_fld_char_type(r).as_deref() == Some("begin") {
                    begin = Some(i);
                    break;
                }
            }
        }
        let Some(b) = begin else { break };
        // scan the field: instruction runs up to separate, value runs, end
        let mut instr = String::new();
        let mut separate = None;
        let mut end = None;
        let mut j = b + 1;
        while j < el.children.len() {
            if let Node::Elem(r) = &el.children[j] {
                if r.name == "w:r" {
                    match run_fld_char_type(r).as_deref() {
                        Some("end") => {
                            end = Some(j);
                            break;
                        }
                        Some("separate") if separate.is_none() => separate = Some(j),
                        // nested field begin: the outer field is malformed
                        Some("begin") => break,
                        _ => {}
                    }
                    if separate.is_none() {
                        instr.push_str(&run_instr_text(r));
                    }
                }
            }
            j += 1;
        }
        let Some(e) = end else { break }; // unterminated field: leave alone
        let value = docproperty_name(&instr).and_then(|n| prop_value(props, &n).map(|v| v.to_string()));
        match value {
            Some(value) => {
                apply_complex_dissolve(el, b, separate, e, &value);
                *changed = true;
                scan_from = b + 1;
            }
            // not a known property field: skip past it
            None => scan_from = e + 1,
        }
    }
}

/// Remove the field machinery of one complex field, keeping a single literal
/// value (docxcompose ComplexField.replace_field_with_value + update).
fn apply_complex_dissolve(
    el: &mut Element,
    begin: usize,
    separate: Option<usize>,
    end: usize,
    value: &str,
) {
    let template = match &el.children[begin] {
        Node::Elem(r) => value_run_template(r),
        _ => Element::new("w:r"),
    };
    match separate {
        Some(s) => {
            let n_value = end - (s + 1);
            // drop the end run, then everything from begin through separate
            el.children.remove(end);
            for _ in begin..=s {
                el.children.remove(begin);
            }
            // the former value runs now start at `begin`
            if n_value == 0 {
                let mut r = template;
                set_run_text(&mut r, value);
                el.children.insert(begin, Node::Elem(r));
            } else {
                if let Node::Elem(r) = &mut el.children[begin] {
                    set_run_text(r, value);
                }
                // additional value runs carrying text would duplicate the value
                let mut remaining = n_value;
                let mut k = 1;
                while k < remaining {
                    let text_run = matches!(&el.children[begin + k], Node::Elem(r) if has_wt(r));
                    if text_run {
                        el.children.remove(begin + k);
                        remaining -= 1;
                    } else {
                        k += 1;
                    }
                }
            }
        }
        None => {
            // no separate: the field has no cached value; replace the whole
            // field by a single value run
            for _ in begin..=end {
                el.children.remove(begin);
            }
            let mut r = template;
            set_run_text(&mut r, value);
            el.children.insert(begin, Node::Elem(r));
        }
    }
}


#[cfg(test)]
mod tests {
    use super::*;

    fn pkg() -> Package {
        Package::from_bytes(&crate::package::minimal_docx("<w:p/>")).unwrap()
    }

    #[test]
    fn test_read_properties_empty_without_part() {
        let p = pkg();
        assert!(read_properties(&p).is_empty());
    }

    #[test]
    fn test_parse_typed_values() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="2" name="s"><vt:lpwstr>hi</vt:lpwstr></property>
<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="3" name="b"><vt:bool>true</vt:bool></property>
<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="4" name="i"><vt:i4>42</vt:i4></property>
<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="5" name="f"><vt:r8>2.5</vt:r8></property>
<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="6" name="d"><vt:filetime>2024-01-02T03:04:05Z</vt:filetime></property>
</Properties>"#;
        let props = parse_custom_properties(xml);
        assert_eq!(
            props,
            vec![
                ("s".to_string(), CustomValue::Str("hi".into())),
                ("b".to_string(), CustomValue::Bool(true)),
                ("i".to_string(), CustomValue::Int(42)),
                ("f".to_string(), CustomValue::Float(2.5)),
                (
                    "d".to_string(),
                    CustomValue::DateTime("2024-01-02T03:04:05Z".into())
                ),
            ]
        );
    }

    #[test]
    fn test_set_creates_part_with_content_type_and_rel() {
        let mut p = pkg();
        assert!(!p.contains(CUSTOM_PROPS_PART));
        set_property(&mut p, "dept", &CustomValue::Str("R&D".into())).unwrap();
        assert!(p.contains(CUSTOM_PROPS_PART));
        let ct = p.get_string("[Content_Types].xml").unwrap();
        assert!(ct.contains("custom-properties+xml"));
        let rels = p.get_string("_rels/.rels").unwrap();
        assert!(rels.contains("relationships/custom-properties"));
        let xml = p.get_string(CUSTOM_PROPS_PART).unwrap();
        assert!(xml.contains("name=\"dept\""));
        assert!(xml.contains("<vt:lpwstr>R&amp;D</vt:lpwstr>"));
        assert!(xml.contains("pid=\"2\""));
        let props = read_properties(&p);
        assert_eq!(props, vec![("dept".to_string(), CustomValue::Str("R&D".into()))]);
    }

    #[test]
    fn test_set_overwrite_pid_increment_and_delete() {
        let mut p = pkg();
        set_property(&mut p, "a", &CustomValue::Int(1)).unwrap();
        set_property(&mut p, "b", &CustomValue::Bool(false)).unwrap();
        set_property(&mut p, "a", &CustomValue::Int(2)).unwrap();
        let props = read_properties(&p);
        assert_eq!(
            props,
            vec![
                ("a".to_string(), CustomValue::Int(2)),
                ("b".to_string(), CustomValue::Bool(false)),
            ]
        );
        let xml = p.get_string(CUSTOM_PROPS_PART).unwrap();
        assert!(xml.contains("pid=\"3\"")); // b got the next pid
        assert!(!delete_property(&mut p, "nope").unwrap());
        assert!(delete_property(&mut p, "a").unwrap());
        assert_eq!(
            read_properties(&p),
            vec![("b".to_string(), CustomValue::Bool(false))]
        );
    }
}
