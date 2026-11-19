//! Recovery of ill-formed rendered XML, in the structural way libxml2 2.14's
//! recover mode does it (lxml `XMLParser(recover=True)`, which docxtpl uses in
//! `fix_tables`), but without losing text.
//!
//! Only the error cases rendered templates produce are covered:
//! - an end tag that does not match closes the current element;
//! - content after the root element ends the document; open elements are
//!   closed at the end;
//! - characters XML forbids, raw or as a character reference, are dropped:
//!   they cannot be represented.
//!
//! Text is kept, unlike libxml2 (intentional divergence): predefined entity
//! references (`&amp;`, `&lt;`, `&gt;`, `&quot;`, `&apos;`) keep their value
//! after the first error, where libxml2 drops them; a `&` that starts no valid
//! reference and a `<` that starts no markup stay as literal text, where
//! libxml2 drops them.
//!
//! Attribute values, comments, processing instructions and CDATA sections are
//! copied as they are.

fn is_name_start(c: char) -> bool { c.is_alphabetic() || c == '_' || c == ':' }
fn is_name_char(c: char) -> bool { is_name_start(c) || c.is_ascii_digit() || matches!(c, '-' | '.') || c.is_alphanumeric() }
fn is_xml_char(c: char) -> bool {
    matches!(c, '\u{9}' | '\u{a}' | '\u{d}' | '\u{20}'..='\u{d7ff}' | '\u{e000}'..='\u{fffd}' | '\u{10000}'..='\u{10ffff}')
}

fn escape_into(out: &mut String, c: char) {
    match c {
        '&' => out.push_str("&amp;"),
        '<' => out.push_str("&lt;"),
        '>' => out.push_str("&gt;"),
        c => out.push(c),
    }
}

enum Reference {
    /// A character XML allows.
    Char(char),
    /// A character reference to a character XML forbids.
    Forbidden,
}

/// The reference starting at `chars[0] == '&'`: how many characters it spans
/// and its value, or `(1, None)` when it is not a valid reference.
fn reference(chars: &[char]) -> (usize, Option<Reference>) {
    if chars.get(1) == Some(&'#') {
        let hex = chars.get(2) == Some(&'x');
        let start = 2 + usize::from(hex);
        let mut end = start;
        while end < chars.len() && (if hex { chars[end].is_ascii_hexdigit() } else { chars[end].is_ascii_digit() }) { end += 1; }
        if end == start || chars.get(end) != Some(&';') { return (1, None); }
        let digits: String = chars[start..end].iter().collect();
        let value = u32::from_str_radix(&digits, if hex { 16 } else { 10 }).ok().and_then(char::from_u32);
        let reference = match value {
            Some(v) if is_xml_char(v) => Reference::Char(v),
            _ => Reference::Forbidden,
        };
        return (end + 1, Some(reference));
    }
    if !chars.get(1).is_some_and(|&n| is_name_start(n)) { return (1, None); }
    let mut end = 2;
    while end < chars.len() && is_name_char(chars[end]) { end += 1; }
    if chars.get(end) != Some(&';') { return (1, None); }
    let name: String = chars[1..end].iter().collect();
    let value = match name.as_str() { "amp" => '&', "lt" => '<', "gt" => '>', "quot" => '"', "apos" => '\'', _ => return (1, None) };
    (end + 1, Some(Reference::Char(value)))
}

/// Whether `xml` parses without well-formedness errors.
pub fn is_well_formed(xml: &str) -> bool {
    use quick_xml::events::Event;
    let mut reader = crate::xml_events::TextRuns::new(crate::xml_events::reader(xml));
    reader.config_mut().check_end_names = true;
    let mut depth = 0usize;
    let mut seen_root = false;
    loop {
        match reader.read_event() {
            Ok(Event::Start(_)) => { if depth == 0 && seen_root { return false; } depth += 1; seen_root = true; }
            Ok(Event::Empty(_)) => { if depth == 0 && seen_root { return false; } seen_root = true; }
            Ok(Event::End(_)) => { if depth == 0 { return false; } depth -= 1; }
            Ok(Event::Text(text)) => {
                if quick_xml::escape::unescape(&text).is_err() { return false; }
                if depth == 0 && !text.bytes().all(|b| b.is_ascii_whitespace()) { return false; }
                if !text.chars().all(is_xml_char) { return false; }
            }
            Ok(Event::Eof) => return depth == 0 && seen_root,
            Ok(_) => {}
            Err(_) => return false,
        }
    }
}

/// How libxml2's recover mode ends a start tag with a malformed attribute.
enum TagEnding {
    /// The tag stays an open element; parsing continues at the index.
    Open(usize),
    /// The element is empty; the rest, from the index, is text.
    Empty(usize),
}

/// For the start tag whose name ends at `start`: when an attribute is
/// malformed, where its valid attributes end and how libxml2 recovers it.
/// An attribute without `=` directly before `>` is dropped and the element
/// stays open; otherwise the element is empty and the text resumes at the
/// next non-blank character (for an unquoted value, at the value).
/// `None` for a well-formed tag or one this does not cover.
fn malformed_start_tag(chars: &[char], start: usize) -> Option<(usize, TagEnding)> {
    let blank = |c: char| matches!(c, ' ' | '\t' | '\r' | '\n');
    let skip = |mut k: usize| { while k < chars.len() && blank(chars[k]) { k += 1; } k };
    let mut valid_end = start;
    let mut k = start;
    loop {
        let next = skip(k);
        match chars.get(next) {
            Some('>') | None => return None,
            Some('/') if chars.get(next + 1) == Some(&'>') => return None,
            Some(&c) if is_name_start(c) && next > k => {
                let mut name_end = next + 1;
                while name_end < chars.len() && is_name_char(chars[name_end]) { name_end += 1; }
                let after = skip(name_end);
                if chars.get(after) != Some(&'=') {
                    return Some((valid_end, match chars.get(after) {
                        Some('>') => TagEnding::Open(after + 1),
                        _ => TagEnding::Empty(after),
                    }));
                }
                let value = skip(after + 1);
                match chars.get(value) {
                    Some(&quote @ ('"' | '\'')) => {
                        let close = (value + 1..chars.len()).find(|&q| chars[q] == quote)?;
                        valid_end = close + 1;
                        k = close + 1;
                    }
                    Some(_) => return Some((valid_end, TagEnding::Empty(value))),
                    None => return None,
                }
            }
            _ => return None,
        }
    }
}

/// Recover `xml` (a document or element fragment) like libxml2's recover mode.
pub fn recover(xml: &str) -> String {
    let chars: Vec<char> = xml.chars().collect();
    let mut out = String::with_capacity(xml.len());
    let mut stack: Vec<String> = Vec::new();
    let mut root_done = false;
    let mut i = 0;
    let starts = |i: usize, s: &str| s.chars().enumerate().all(|(k, c)| chars.get(i + k) == Some(&c));
    let find = |from: usize, s: &str| -> Option<usize> { (from..chars.len()).find(|&j| starts(j, s)) };
    while i < chars.len() {
        let c = chars[i];
        if c == '<' {
            if starts(i, "<!--") {
                let end = find(i + 4, "-->").map_or(chars.len(), |e| e + 3);
                out.extend(&chars[i..end]);
                i = end;
                continue;
            }
            if starts(i, "<![CDATA[") {
                let end = find(i + 9, "]]>").map_or(chars.len(), |e| e + 3);
                if root_done { break; }
                out.extend(&chars[i..end]);
                i = end;
                continue;
            }
            if starts(i, "<?") {
                let end = find(i + 2, "?>").map_or(chars.len(), |e| e + 2);
                out.extend(&chars[i..end]);
                i = end;
                continue;
            }
            if starts(i, "<!") {
                let end = find(i + 2, ">").map_or(chars.len(), |e| e + 1);
                out.extend(&chars[i..end]);
                i = end;
                continue;
            }
            if chars.get(i + 1) == Some(&'/') {
                let end = find(i + 2, ">").map_or(chars.len(), |e| e + 1);
                i = end;
                // Any end tag closes the current element; a stray one is dropped.
                if let Some(open) = stack.pop() {
                    out.push_str("</");
                    out.push_str(&open);
                    out.push('>');
                    if stack.is_empty() { root_done = true; }
                }
                continue;
            }
            if chars.get(i + 1).is_some_and(|&n| is_name_start(n)) {
                if root_done { break; }
                let mut j = i + 1;
                while j < chars.len() && is_name_char(chars[j]) { j += 1; }
                let name: String = chars[i + 1..j].iter().collect();
                // A malformed attribute ends the tag as libxml2 recovers it.
                if let Some((attributes_end, ending)) = malformed_start_tag(&chars, j) {
                    out.extend(&chars[i..attributes_end]);
                    match ending {
                        TagEnding::Open(next) => {
                            out.push('>');
                            stack.push(name);
                            i = next;
                        }
                        TagEnding::Empty(next) => {
                            out.push_str("/>");
                            if stack.is_empty() { root_done = true; }
                            i = next;
                        }
                    }
                    continue;
                }
                // attributes: copy through the closing '>' honoring quotes
                let mut quote: Option<char> = None;
                let mut k = j;
                while k < chars.len() {
                    match (quote, chars[k]) {
                        (None, '"' | '\'') => quote = Some(chars[k]),
                        (Some(q), c) if c == q => quote = None,
                        (None, '>') => break,
                        _ => {}
                    }
                    k += 1;
                }
                let end = (k + 1).min(chars.len());
                let empty = k > 0 && chars.get(k - 1) == Some(&'/');
                out.extend(&chars[i..end]);
                i = end;
                if !empty { stack.push(name); } else if stack.is_empty() { root_done = true; }
                continue;
            }
            // '<' that starts no markup is text
            if !root_done && !stack.is_empty() { out.push_str("&lt;"); }
            i += 1;
            continue;
        }
        if root_done || stack.is_empty() {
            if !c.is_whitespace() && root_done { break; }
            if !root_done && stack.is_empty() { out.push(c); }
            i += 1;
            continue;
        }
        if c == '&' {
            let (consumed, value) = reference(&chars[i..]);
            match value {
                Some(Reference::Char(v)) => escape_into(&mut out, v),
                Some(Reference::Forbidden) => {}
                // Not a valid reference: the `&` is text.
                None => out.push_str("&amp;"),
            }
            i += consumed;
            continue;
        }
        if !is_xml_char(c) {
            i += 1;
            continue;
        }
        escape_into(&mut out, c);
        i += 1;
    }
    while let Some(open) = stack.pop() {
        out.push_str("</");
        out.push_str(&open);
        out.push('>');
    }
    out
}

/// docxtpl recovers only the rendered body (`fix_tables` parses the body
/// element string); recover the `<w:body>` element of a whole document.
pub fn recover_document_body(document: &str) -> String {
    let (Some(start), Some(end)) = (document.find("<w:body"), document.rfind("</w:body>")) else { return recover(document) };
    let end = end + "</w:body>".len();
    if end < start { return recover(document); }
    format!("{}{}{}", &document[..start], recover(&document[start..end]), &document[end..])
}

/// `recover_document_body` for a whole document whose body may repeat the
/// root's namespace declarations: those are dropped, as when a body is put
/// back into its document.
pub fn recover_document(document: &str) -> String {
    let recovered = recover_document_body(document);
    let (Some(start), Some(end)) = (recovered.find("<w:body"), recovered.rfind("</w:body>")) else { return recovered };
    let end = end + "</w:body>".len();
    if end < start { return recovered; }
    match crate::xmldom::adopt_body_namespaces(&recovered, &recovered[start..end]) {
        Some(body) => format!("{}{}{}", &recovered[..start], body, &recovered[end..]),
        None => recovered,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Expected strings come from lxml 6 / libxml2 2.14 recover mode
    // (etree.tostring(etree.fromstring(x, XMLParser(recover=True)))).
    #[test]
    fn mismatched_end_tags_close_the_current_element() {
        assert_eq!(recover("<d><p><r><t>a<tab/></tabs><x/></pPr><y/></r></p></d>"), "<d><p><r><t>a<tab/></t><x/></r><y/></p></d>");
        assert_eq!(recover("<d><p><t>Ana <ltda> em X</t></r></p><s/></d>"), "<d><p><t>Ana <ltda> em X</ltda></t></p><s/></d>");
    }

    /// libxml2 drops these after the first error (`a  b`, `e  f`); text is
    /// kept here on purpose.
    #[test]
    fn entities_after_an_error_are_kept() {
        assert_eq!(recover("<a>x &amp; y &lt; z &#38; w</a>"), "<a>x &amp; y &lt; z &amp; w</a>");
        assert_eq!(recover("<a><t>c &amp; d</t><t>a & b</t><t>e &amp; f</t></a>"), "<a><t>c &amp; d</t><t>a &amp; b</t><t>e &amp; f</t></a>");
        // libxml2: "<a><t>A  D ZZ; E A</t></a>"
        assert_eq!(recover("<a><t>A &x D &#xZZ; E &#65; &copy;</t></a>"), "<a><t>A &amp;x D &amp;#xZZ; E A &amp;copy;</t></a>");
        assert_eq!(recover("<a><t>&quot;q&apos; &gt;</t></a>"), "<a><t>\"q' &gt;</t></a>");
    }

    /// A start tag with an attribute that has no quoted value, as a rendered
    /// object `repr` (`<reversed object at 0x...>`) produces.
    #[test]
    fn malformed_attributes_end_the_start_tag() {
        assert_eq!(recover("<a><t><reversed object at 0x1>x</t></a>"), "<a><t><reversed/>at 0x1&gt;x</t></a>");
        assert_eq!(recover("<a><t><b c=\"1\" object at 0x1>x</t></a>"), "<a><t><b c=\"1\"/>at 0x1&gt;x</t></a>");
        assert_eq!(recover("<a><t><b obj>x</t><u/></a>"), "<a><t><b>x</b><u/></t></a>");
        assert_eq!(recover("<a><t><b obj  more>x</t></a>"), "<a><t><b/>more&gt;x</t></a>");
        assert_eq!(recover("<a><t><b c=d>x</t></a>"), "<a><t><b/>d&gt;x</t></a>");
        assert_eq!(recover("<a><t>pre <list_reverseiterator object at 0x10>post</t><u/></a>"),
                   "<a><t>pre <list_reverseiterator/>at 0x10&gt;post</t><u/></a>");
        // Well-formed tags are unchanged.
        assert_eq!(recover("<a><t c=\"1\" d='2'>x</t><u e=\"3\"/></a>"), "<a><t c=\"1\" d='2'>x</t><u e=\"3\"/></a>");
    }

    #[test]
    fn recovered_document_body_drops_repeated_namespaces() {
        let document = "<w:document xmlns:w=\"urn:w\"><w:body xmlns:w=\"urn:w\"><w:p><w:t><b obj at 0x1></w:t></w:p></w:body></w:document>";
        assert_eq!(recover_document(document),
                   "<w:document xmlns:w=\"urn:w\"><w:body><w:p><w:t><b/>at 0x1&gt;</w:t></w:p></w:body></w:document>");
    }

    #[test]
    fn stray_markup_and_trailing_content() {
        // libxml2: "<a><t>a  b 1 c</t></a>"
        assert_eq!(recover("<a><t>a < b <1 c</t></a>"), "<a><t>a &lt; b &lt;1 c</t></a>");
        assert_eq!(recover("<a><t>x</t></a><extra/>"), "<a><t>x</t></a>");
        assert_eq!(recover("<a><t>unclosed"), "<a><t>unclosed</t></a>");
        assert_eq!(recover("<a><t>a\u{1}b &#1; c</t></a>"), "<a><t>ab  c</t></a>");
    }
}

/// Well-formedness of text with references, as quick-xml 0.36 judged it.
#[cfg(test)]
mod reference_tests {
    use super::*;

    #[test]
    fn text_references_are_checked_per_run() {
        assert!(is_well_formed("<a>x &amp; &lt;&gt;&quot;&apos;&#38;&#x26;</a>"));
        assert!(is_well_formed("<a>&amp;<b/>&lt;</a>"));
        assert!(!is_well_formed("<a>R&D</a>"));
        assert!(!is_well_formed("<a>&foo;</a>"));
        assert!(!is_well_formed("<a>&amp; &#0;</a>"));
        assert!(!is_well_formed("<a>&amp</a>"));
        assert!(!is_well_formed("<a/>&amp;"));
        assert!(!is_well_formed("&#32;<a/>"));
        assert!(is_well_formed(" \n<a/>\n"));
    }
}
