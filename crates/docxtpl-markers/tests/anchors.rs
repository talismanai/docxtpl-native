//! In-line markers moved to paragraph anchors, markers at a paragraph's
//! edges left alone, and markers placed by counting back from another (as
//! anchored offsets, or as bookmarks of their own).

use docxtpl_markers::{Naming, RelativeMarker, anchor_inline_markers, place_relative_markers, strip_markers};
use docxtplrs::package::{Package, minimal_docx};

fn naming() -> Naming {
    Naming { marker_prefix: "MARK".into(), anchor_prefix: "MARKP".into(), first_anchor_id: 3_000_000 }
}

fn bookmark(id: u32, name: &str) -> String {
    format!(r#"<w:bookmarkStart w:id="{id}" w:name="{name}"/><w:bookmarkEnd w:id="{id}"/>"#)
}

fn document(body: &str) -> Vec<u8> {
    minimal_docx(body)
}

#[test]
fn moves_a_marker_inside_a_line_to_its_paragraph_anchor() {
    let body = format!(
        r#"<w:p><w:r><w:t xml:space="preserve">before </w:t></w:r>{}<w:r><w:t>after</w:t></w:r></w:p>"#,
        bookmark(1, "MARKS0")
    );
    let anchored = anchor_inline_markers(&document(&body), &naming(), &[]).expect("anchored");
    let [(name, marker)] = anchored.inline_markers.as_slice() else {
        panic!("{:?}", anchored.inline_markers)
    };
    assert_eq!(name, "MARKS0");
    assert_eq!(marker.paragraph_anchor, "MARKP3000000");
    // "before " contributes six non-whitespace characters; the space does not.
    assert_eq!(marker.offset, 6);
    assert_eq!(marker.paragraph_text, "beforeafter");
    // The marker is gone from the document and the anchor took its place.
    let xml = Package::from_bytes(&anchored.content).expect("package")
        .get_string("word/document.xml").expect("document");
    assert!(!xml.contains("MARKS0"), "{xml}");
    assert!(xml.contains("MARKP3000000"), "{xml}");
}

#[test]
fn leaves_a_marker_with_nothing_on_one_side_where_it_is() {
    // Nothing at all before it: the marker does not split a line, so moving
    // it would be a change for nothing.
    let body = format!(r#"<w:p>{}<w:r><w:t>value</w:t></w:r></w:p>"#, bookmark(1, "MARKS0"));
    let anchored = anchor_inline_markers(&document(&body), &naming(), &[]).expect("anchored");
    assert!(anchored.inline_markers.is_empty());
    let xml = Package::from_bytes(&anchored.content).expect("package")
        .get_string("word/document.xml").expect("document");
    assert!(xml.contains("MARKS0"), "{xml}");
}

/// A value whose start marker the render did not keep: only its end marker
/// is left, and the caller knows how many characters the value has.
const LOST_START: &str = r#"<w:p><w:r><w:t xml:space="preserve">one </w:t><w:t>two</w:t></w:r><w:bookmarkStart w:id="2" w:name="MARKE0"/><w:bookmarkEnd w:id="2"/></w:p>"#;

fn lost_start(chars_before: usize) -> RelativeMarker {
    RelativeMarker { name: "MARKS0".into(), relative_to: "MARKE0".into(), chars_before }
}

#[test]
fn places_a_marker_by_counting_back_from_another() {
    let anchored = anchor_inline_markers(&document(LOST_START), &naming(), &[lost_start(3)]).expect("anchored");
    let [(name, marker)] = anchored.inline_markers.as_slice() else {
        panic!("{:?}", anchored.inline_markers)
    };
    assert_eq!(name, "MARKS0");
    assert_eq!(marker.paragraph_text, "onetwo");
    // Six characters before the end marker, the value's three: the start
    // falls just before "two".
    assert_eq!(marker.offset, 3);
}

#[test]
fn clamps_a_placed_marker_to_the_paragraph_start() {
    let anchored = anchor_inline_markers(&document(LOST_START), &naming(), &[lost_start(500)]).expect("anchored");
    assert_eq!(anchored.inline_markers[0].1.offset, 0);
}

#[test]
fn ignores_a_placed_marker_whose_target_is_elsewhere() {
    let elsewhere = RelativeMarker { name: "MARKS9".into(), relative_to: "MARKE9".into(), chars_before: 3 };
    let anchored = anchor_inline_markers(&document(LOST_START), &naming(), &[elsewhere]).expect("anchored");
    assert!(anchored.inline_markers.is_empty());
}

#[test]
fn strips_every_marker_and_leaves_other_bookmarks() {
    let body = format!(
        r#"<w:p>{}<w:r><w:t>value</w:t></w:r>{}</w:p>"#,
        bookmark(1, "MARKS0"),
        bookmark(2, "_Toc1")
    );
    let stripped = strip_markers(&document(&body), &naming()).expect("stripped");
    let xml = Package::from_bytes(&stripped).expect("package")
        .get_string("word/document.xml").expect("document");
    assert!(!xml.contains("MARKS0"), "{xml}");
    assert!(xml.contains("_Toc1"), "{xml}");
    assert!(xml.contains(">value<"), "{xml}");
}

/// `w:t` text and bookmark names (as `[name]`) of `word/document.xml`, with
/// `|` between runs.
fn flow(docx: &[u8]) -> String {
    let xml = Package::from_bytes(docx).expect("package").get_string("word/document.xml").expect("document");
    let mut out = String::new();
    let mut rest = xml.as_str();
    while let Some(at) = rest.find('<') {
        let end = rest[at..].find('>').map_or(rest.len(), |i| at + i + 1);
        let tag = &rest[at..end];
        if (tag == "<w:t>" || tag.starts_with("<w:t ")) && !tag.ends_with("/>") {
            let close = rest[end..].find("</w:t>").map_or(rest.len(), |i| end + i);
            out.push_str(&rest[end..close]);
            rest = &rest[close..];
            continue;
        }
        if tag.starts_with("<w:bookmarkStart") {
            if let Some(name) = tag.split("w:name=\"").nth(1).and_then(|s| s.split('"').next()) {
                out.push_str(&format!("[{name}]"));
            }
        } else if tag == "</w:r>" {
            out.push('|');
        }
        rest = &rest[end..];
    }
    out
}

#[test]
fn inserts_a_lost_marker_where_its_characters_start() {
    // Three characters before the end marker: inside the run, between its
    // two texts, which the run is split at.
    let placed = place_relative_markers(&document(LOST_START), &naming(), &[lost_start(3)]).expect("placed");
    assert_eq!(flow(&placed), "one |[MARKS0]two|[MARKE0]");
    // Inside a text: "one two" split before "two", its space kept before.
    let body = r#"<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>one two</w:t></w:r><w:bookmarkStart w:id="2" w:name="MARKE0"/><w:bookmarkEnd w:id="2"/></w:p>"#;
    let placed = place_relative_markers(&document(body), &naming(), &[lost_start(3)]).expect("placed");
    assert_eq!(flow(&placed), "one |[MARKS0]two|[MARKE0]");
    let xml = Package::from_bytes(&placed).expect("package").get_string("word/document.xml").expect("document");
    // Both halves keep the run's properties.
    assert_eq!(xml.matches("<w:b/>").count(), 2, "{xml}");
    assert!(xml.contains(r#"w:id="3000000""#), "{xml}");
}

#[test]
fn inserts_a_lost_marker_before_a_run_it_opens() {
    // All six characters: the marker opens the paragraph's first run, which
    // stays whole; asking for more stops there too.
    for chars in [6, 500] {
        let placed = place_relative_markers(&document(LOST_START), &naming(), &[lost_start(chars)]).expect("placed");
        assert_eq!(flow(&placed), "[MARKS0]one two|[MARKE0]");
    }
    // None: right before the marker counted from.
    let placed = place_relative_markers(&document(LOST_START), &naming(), &[lost_start(0)]).expect("placed");
    assert_eq!(flow(&placed), "one two|[MARKS0][MARKE0]");
}

#[test]
fn leaves_the_document_alone_when_nothing_is_placed() {
    let input = document(LOST_START);
    let elsewhere = RelativeMarker { name: "MARKS9".into(), relative_to: "MARKE9".into(), chars_before: 3 };
    assert_eq!(place_relative_markers(&input, &naming(), &[elsewhere]).expect("placed"), input);
    assert_eq!(place_relative_markers(&input, &naming(), &[]).expect("placed"), input);
}
