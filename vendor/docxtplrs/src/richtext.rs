//! Port of docxtpl RichText / RichTextParagraph / Listing XML generation.

/// html escape like Python's html.escape (escapes & < > " ')
pub fn html_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            _ => out.push(c),
        }
    }
    out
}

#[derive(Debug, Clone, Default)]
pub struct TextProps {
    pub style: Option<String>,
    pub color: Option<String>,
    pub highlight: Option<String>,
    pub size: Option<u32>,
    pub subscript: bool,
    pub superscript: bool,
    pub bold: bool,
    pub italic: bool,
    /// false = no underline; Some string holds underline value ("single" etc.) or bool-ish
    pub underline: Option<String>,
    pub strike: bool,
    pub font: Option<String>,
    pub url_id: Option<String>,
    pub rtl: bool,
    pub lang: Option<String>,
}

pub fn richtext_run(text: &str, p: &TextProps) -> String {
    use std::fmt::Write;

    // Write directly into the final XML buffer. In particular, the common
    // no-markup case does not allocate an intermediate escaped string.
    let mut xml = String::with_capacity(text.len().saturating_add(96));
    if let Some(url_id) = &p.url_id {
        let _ = write!(xml, "<w:hyperlink r:id=\"{}\" w:tgtFrame=\"_blank\">", url_id);
    }
    xml.push_str("<w:r>");
    let has_properties = p.style.is_some() || p.font.is_some()
        || p.color.is_some() || p.highlight.is_some() || p.size.is_some()
        || p.subscript || p.superscript || p.bold || p.italic
        || p.underline.is_some() || p.strike || p.rtl || p.lang.is_some();
    if has_properties {
        xml.push_str("<w:rPr>");
        if let Some(style) = &p.style {
            let _ = write!(xml, "<w:rStyle w:val=\"{}\"/>", style);
        }
        if let Some(font) = &p.font {
            if let Some((region, name)) = font.split_once(':') {
                let _ = write!(xml, "<w:rFonts w:ascii=\"{name}\" w:hAnsi=\"{name}\" w:cs=\"{name}\" w:{region}=\"{name}\"/>");
            } else {
                let _ = write!(xml, "<w:rFonts w:ascii=\"{font}\" w:hAnsi=\"{font}\" w:cs=\"{font}\"/>");
            }
        }
        if let Some(color) = &p.color {
            let _ = write!(xml, "<w:color w:val=\"{}\"/>", color.strip_prefix('#').unwrap_or(color));
        }
        if let Some(highlight) = &p.highlight {
            let _ = write!(xml, "<w:shd w:fill=\"{}\"/>", highlight.strip_prefix('#').unwrap_or(highlight));
        }
        if let Some(size) = p.size {
            let _ = write!(xml, "<w:sz w:val=\"{size}\"/><w:szCs w:val=\"{size}\"/>");
        }
        if p.subscript { xml.push_str("<w:vertAlign w:val=\"subscript\"/>"); }
        if p.superscript { xml.push_str("<w:vertAlign w:val=\"superscript\"/>"); }
        if p.bold {
            xml.push_str("<w:b/>");
            if p.rtl { xml.push_str("<w:bCs/>"); }
        }
        if p.italic {
            xml.push_str("<w:i/>");
            if p.rtl { xml.push_str("<w:iCs/>"); }
        }
        if let Some(underline) = &p.underline {
            let valid = ["single", "double", "thick", "dotted", "dash", "dotDash", "dotDotDash", "wave"];
            let value = if valid.contains(&underline.as_str()) { underline.as_str() } else { "single" };
            let _ = write!(xml, "<w:u w:val=\"{value}\"/>");
        }
        if p.strike { xml.push_str("<w:strike/>"); }
        if p.rtl { xml.push_str("<w:rtl w:val=\"true\"/>"); }
        if let Some(lang) = &p.lang {
            let _ = write!(xml, "<w:lang w:val=\"{}\"/>", lang);
        }
        xml.push_str("</w:rPr>");
    }
    xml.push_str("<w:t xml:space=\"preserve\">");
    let mut start = 0;
    for (index, byte) in text.bytes().enumerate() {
        let escaped = match byte {
            b'&' => "&amp;",
            b'<' => "&lt;",
            b'>' => "&gt;",
            b'"' => "&quot;",
            b'\'' => "&#x27;",
            _ => continue,
        };
        // Escapable bytes are ASCII and thus always UTF-8 boundaries.
        xml.push_str(&text[start..index]);
        xml.push_str(escaped);
        start = index + 1;
    }
    xml.push_str(&text[start..]);
    xml.push_str("</w:t></w:r>");
    if p.url_id.is_some() { xml.push_str("</w:hyperlink>"); }
    xml
}

pub fn richtext_paragraph(runs_xml: &str, parastyle: Option<&str>) -> String {
    let mut prop = String::new();
    if let Some(ps) = parastyle {
        prop += &format!("<w:pStyle w:val=\"{}\"/>", ps);
    }
    let mut xml = String::from("<w:p>");
    if !prop.is_empty() {
        xml += &format!("<w:pPr>{}</w:pPr>", prop);
    }
    xml += runs_xml;
    xml += "</w:p>";
    xml
}

pub fn listing_xml(text: &str) -> String {
    html_escape(text)
}

#[cfg(test)]
mod tests {
    use super::{html_escape, richtext_run, TextProps};

    #[test]
    fn streamed_text_matches_escaping_for_unicode_and_markup() {
        for text in ["", "plain", "façade 🐍", "<&>\"'", "α&🐍<é", "\0&\n"] {
            let expected = format!("<w:r><w:t xml:space=\"preserve\">{}</w:t></w:r>", html_escape(text));
            assert_eq!(richtext_run(text, &TextProps::default()), expected);
        }
    }
}
