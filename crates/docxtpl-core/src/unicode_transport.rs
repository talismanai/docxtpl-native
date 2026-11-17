//! Experimental transport for XML output containing Python Unicode code points.
//!
//! Deferred values stay typed; text is never interpreted as an existing
//! deferred marker.

use std::collections::HashSet;
use std::fmt::Write as _;

/// Input to the Unicode transformation boundary.
pub enum Fragment<'a> {
    Text(&'a [u32]),
    Deferred(usize),
}

/// Output with surrogate code points and deferred values preserved separately.
#[derive(Debug, PartialEq, Eq)]
pub enum OwnedFragment {
    Text(Vec<u32>),
    Deferred(usize),
}

fn transform(source: String) -> String {
    let source = docxtplrs::template::remove_paragraph_newlines(&source).into_owned();
    let source = docxtplrs::template::restore_escaped_delims(source);
    docxtplrs::patch::resolve_listing(&source).into_owned()
}

fn write_text(output: &mut String, text: &[u32], prefix: Option<&str>) -> Result<(), String> {
    for &code in text {
        if (0xd800..=0xdfff).contains(&code) {
            if let Some(prefix) = prefix {
                write!(output, "{prefix}S{code:X}\u{2}").map_err(|error| error.to_string())?;
            } else {
                output.push_str("\u{1}X\u{2}");
            }
        } else {
            output.push(char::from_u32(code).ok_or("invalid Unicode code point")?);
        }
    }
    Ok(())
}

/// Apply the existing document-output transforms without confusing user text
/// with generated transport tokens. The caller must materialize deferred values
/// and choose the appropriate XML recovery procedure afterwards.
pub fn transform_fragments(fragments: &[Fragment<'_>]) -> Result<Vec<OwnedFragment>, String> {
    let mut probe = String::new();
    for fragment in fragments {
        match fragment {
            Fragment::Text(text) => write_text(&mut probe, text, None)?,
            Fragment::Deferred(_) => probe.push_str("\u{1}X\u{2}"),
        }
    }
    let probe = transform(probe);
    let mut occupied = HashSet::new();
    let bytes = probe.as_bytes();
    for start in memchr::memchr_iter(1, bytes) {
        let mut end = start + 1;
        while bytes.get(end).is_some_and(u8::is_ascii_digit) { end += 1; }
        if end > start + 1 && bytes.get(end) == Some(&b'Z') {
            if let Ok(nonce) = probe[start + 1..end].parse::<u64>() { occupied.insert(nonce); }
        }
    }
    let mut nonce = 0u64;
    while occupied.contains(&nonce) {
        nonce = nonce.checked_add(1).ok_or("no available Unicode transport prefix")?;
    }
    let prefix = format!("\u{1}{nonce}Z");
    let mut encoded = String::new();
    for fragment in fragments {
        match fragment {
            Fragment::Text(text) => write_text(&mut encoded, text, Some(&prefix))?,
            Fragment::Deferred(index) => write!(&mut encoded, "{prefix}D{index}\u{2}")
                .map_err(|error| error.to_string())?,
        }
    }
    let transformed = transform(encoded);
    let mut result = Vec::new();
    let mut text = Vec::new();
    let mut rest = transformed.as_str();
    while let Some(start) = rest.find(&prefix) {
        text.extend(rest[..start].chars().map(u32::from));
        let after = &rest[start + prefix.len()..];
        let end = after.find('\u{2}').ok_or("damaged Unicode transport token")?;
        let token = &after[..end];
        if let Some(value) = token.strip_prefix('S') {
            let code = u32::from_str_radix(value, 16).map_err(|_| "invalid surrogate token")?;
            if !(0xd800..=0xdfff).contains(&code) { return Err("invalid surrogate token".into()); }
            text.push(code);
        } else if let Some(value) = token.strip_prefix('D') {
            if !text.is_empty() { result.push(OwnedFragment::Text(std::mem::take(&mut text))); }
            result.push(OwnedFragment::Deferred(value.parse().map_err(|_| "invalid deferred token")?));
        } else {
            return Err("invalid Unicode transport token".into());
        }
        rest = &after[end + 1..];
    }
    text.extend(rest.chars().map(u32::from));
    if !text.is_empty() { result.push(OwnedFragment::Text(text)); }
    Ok(result)
}

/// Transform and materialize host fragments without converting surrogates to
/// UTF-8. Deferred values are resolved at most once, as in the normal path.
pub fn materialize_fragments(
    core: &mut docxtplrs::template::TplCore,
    part: &str,
    fragments: &[docxtplrs::template::OwnedMaterializationFragment],
) -> Result<Vec<u32>, String> {
    use docxtplrs::template::OwnedMaterializationFragment as Input;
    let converted = fragments.iter().map(|fragment| match fragment {
        Input::Text(text) => Some(text.chars().map(u32::from).collect::<Vec<_>>()),
        _ => None,
    }).collect::<Vec<_>>();
    let mut input = Vec::with_capacity(fragments.len());
    for (fragment, converted) in fragments.iter().zip(&converted) {
        input.push(match fragment {
            Input::Text(_) => Fragment::Text(converted.as_deref().expect("text was converted")),
            Input::Unicode(text) => Fragment::Text(text),
            Input::Deferred(index) if *index < core.deferred.len() => Fragment::Deferred(*index),
            Input::Deferred(index) => return Err(format!("unknown deferred index {index}")),
        });
    }
    let pieces = transform_fragments(&input)?;
    let mut materialized = vec![None; core.deferred.len()];
    let mut result = Vec::new();
    for piece in pieces {
        match piece {
            OwnedFragment::Text(text) => result.extend(text),
            OwnedFragment::Deferred(index) => {
                let slot = materialized.get_mut(index).ok_or_else(|| format!("unknown deferred index {index}"))?;
                if slot.is_none() {
                    *slot = Some(core.materialize_one(part, index)?);
                }
                result.extend(slot.as_ref().expect("just materialized").chars().map(u32::from));
            }
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(value: &str) -> Vec<u32> { value.chars().map(u32::from).collect() }

    #[test]
    fn preserves_surrogates_and_deferred_values_without_parsing_user_text() {
        let first = text("<w:t>\u{1}0ZD7\u{2}");
        let middle = [0xd800, 0xdc00, u32::from('x'), 0xdfff];
        let last = text("\u{1}1ZSDBAD\u{2}</w:t>");
        let output = transform_fragments(&[
            Fragment::Text(&first), Fragment::Text(&middle), Fragment::Deferred(7), Fragment::Text(&last),
        ]).unwrap();
        let expected_first = first.iter().chain(middle.iter()).copied().collect::<Vec<_>>();
        assert_eq!(output, vec![OwnedFragment::Text(expected_first), OwnedFragment::Deferred(7), OwnedFragment::Text(last)]);
    }

    #[test]
    fn detects_prefixes_created_across_fragment_boundaries() {
        let first = text("<w:t>\u{1}");
        let second = text("0ZD0\u{2}</w:t>");
        let output = transform_fragments(&[Fragment::Text(&first), Fragment::Text(&second), Fragment::Deferred(2)]).unwrap();
        assert_eq!(output, vec![OwnedFragment::Text(text("<w:t>\u{1}0ZD0\u{2}</w:t>")), OwnedFragment::Deferred(2)]);
    }

    #[test]
    fn applies_existing_xml_output_transforms_to_wide_text() {
        let source = text("\n<w:p><w:r><w:t>{_{x}_}</w:t></w:r></w:p>");
        let output = transform_fragments(&[Fragment::Text(&source)]).unwrap();
        assert_eq!(output, vec![OwnedFragment::Text(text("<w:p><w:r><w:t>{{x}}</w:t></w:r></w:p>"))]);
    }

    #[test]
    fn listing_breaks_preserve_wide_text_and_deferred_order() {
        let first = text("<w:p><w:r><w:t>A\n");
        let surrogate = [0xd800];
        let last = text("B</w:t></w:r></w:p>");
        let output = transform_fragments(&[
            Fragment::Text(&first), Fragment::Text(&surrogate), Fragment::Deferred(2), Fragment::Text(&last),
        ]).unwrap();
        let mut expected = text("<w:p><w:r><w:t>A</w:t><w:br/><w:t xml:space=\"preserve\">");
        expected.push(0xd800);
        assert_eq!(output, vec![OwnedFragment::Text(expected), OwnedFragment::Deferred(2), OwnedFragment::Text(last)]);
    }

    #[test]
    fn rejects_code_points_outside_unicode_range() {
        assert!(transform_fragments(&[Fragment::Text(&[0x110000])]).is_err());
    }
}
