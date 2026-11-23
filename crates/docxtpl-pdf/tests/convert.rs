//! Conversion with fonts the test carries, the page positions of a
//! document's markers, links over bookmarked ranges, and the font lookups a
//! host is asked to answer.

use std::sync::Mutex;

use docxtpl_pdf::{Compression, MarkedPdf, MarkerPosition, MarkerSide, PdfOptions, RangeLink};
use docxtplrs::package::minimal_docx;

/// A synthetic font from docxide-pdf's tests (family "NB Hyphen Fixture ascii").
const FIXTURE_FONT: &[u8] = include_bytes!("fixtures/nb-hyphen-ascii.ttf");
const FIXTURE_FAMILY: &str = "NB Hyphen Fixture ascii";
const LONG: &str = "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat.";

/// The converter's font registry and its request list are per thread but the
/// tests share one: they take turns, each setting up what it needs.
static FONTS: Mutex<()> = Mutex::new(());

/// A letter page with one-inch margins, so the assertions can talk in points.
const SECTION: &str = r#"<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>"#;

fn run(family: &str, text: &str) -> String {
    format!(
        r#"<w:p><w:r><w:rPr><w:rFonts w:ascii="{family}" w:hAnsi="{family}"/></w:rPr><w:t xml:space="preserve">{text}</w:t></w:r></w:p>"#
    )
}

fn marked(start: &str, family: &str, text: &str, end: &str) -> String {
    format!(
        r#"<w:p><w:bookmarkStart w:id="1" w:name="{start}"/><w:bookmarkEnd w:id="1"/><w:r><w:rPr><w:rFonts w:ascii="{family}" w:hAnsi="{family}"/></w:rPr><w:t xml:space="preserve">{text}</w:t></w:r><w:bookmarkStart w:id="2" w:name="{end}"/><w:bookmarkEnd w:id="2"/></w:p>"#
    )
}

/// Markers named `S…` open a paragraph and `E…` close it.
fn side(name: &str) -> Option<MarkerSide> {
    match name.strip_prefix("MARK")? {
        rest if rest.starts_with('E') => Some(MarkerSide::End),
        _ => Some(MarkerSide::Start),
    }
}

fn position(converted: &MarkedPdf, name: &str) -> MarkerPosition {
    converted
        .markers
        .iter()
        .find(|(n, _)| n == name)
        .unwrap_or_else(|| panic!("{name} not placed"))
        .1
}

/// Only the fonts the test registers, so the result does not depend on the
/// machine's font set.
fn only_fixture_fonts() {
    docxtpl_pdf::use_system_fonts(false);
    docxtpl_pdf::clear_fonts();
    assert!(
        docxtpl_pdf::register_font(FIXTURE_FONT) > 0,
        "the fixture font has no usable face"
    );
    let _ = docxtpl_pdf::font_requests();
}

#[test]
fn converts_a_document_with_a_font_the_host_supplied() {
    let _fonts = FONTS
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    only_fixture_fonts();
    let document = minimal_docx(&format!("{}{SECTION}", run(FIXTURE_FAMILY, "Title")));
    let pdf = docxtpl_pdf::docx_to_pdf(&document).expect("convert");
    assert!(pdf.starts_with(b"%PDF"), "not a PDF");
    // The registered face answered the lookup for its own family. The
    // converter also probes a fallback chain for characters outside it, and
    // those lookups are reported: a host answers the ones it can.
    let requests = docxtpl_pdf::font_requests_json();
    assert!(!requests.contains(FIXTURE_FAMILY), "{requests}");
}

#[test]
fn places_a_start_marker_on_the_first_line_and_an_end_marker_on_the_last() {
    let _fonts = FONTS
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    only_fixture_fonts();
    let document = minimal_docx(&format!(
        "{}{}{}{SECTION}",
        run(FIXTURE_FAMILY, "Title"),
        marked("MARKS0", FIXTURE_FAMILY, LONG, "MARKE0"),
        run(FIXTURE_FAMILY, "After"),
    ));
    let converted = docxtpl_pdf::docx_to_pdf_marked(&document, side).expect("convert");
    assert!(converted.pdf.starts_with(b"%PDF"));
    let start = position(&converted, "MARKS0");
    let end = position(&converted, "MARKE0");
    assert_eq!((start.page, end.page), (0, 0));
    // y grows upwards: the paragraph starts below the title, under the top
    // margin of an 11in page, and it wraps, so its last line is lower.
    assert!(start.y_from_bottom < 792.0 - 72.0, "{start:?}");
    assert!(
        end.y_from_bottom < start.y_from_bottom - 10.0,
        "end {end:?} start {start:?}"
    );
    // A start marker opens its line, an end marker closes it.
    assert_eq!(start.x, 0.0);
    assert!(end.x > 1000.0, "{end:?}");
    // The positions come from the bookmarks, which never reach the PDF.
    assert!(!String::from_utf8_lossy(&converted.pdf).contains("MARKS0"));
}

#[test]
fn reports_only_the_bookmarks_the_caller_claims() {
    let _fonts = FONTS
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    only_fixture_fonts();
    let document = minimal_docx(&format!(
        "{}{SECTION}",
        marked("_Toc1", FIXTURE_FAMILY, "Chapter", "MARKE1"),
    ));
    let converted = docxtpl_pdf::docx_to_pdf_marked(&document, side).expect("convert");
    assert!(
        converted.markers.iter().all(|(name, _)| name == "MARKE1"),
        "{:?}",
        converted.markers
    );
    let json = converted.markers_json();
    assert!(
        json.get("MARKE1").and_then(|m| m.get("page")).is_some(),
        "{json}"
    );
    assert!(json.get("_Toc1").is_none(), "{json}");
}

#[test]
fn asks_the_host_for_a_font_it_does_not_have() {
    let _fonts = FONTS
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    only_fixture_fonts();
    let document = minimal_docx(&format!(
        "{}{SECTION}",
        run("Absent Fixture Family", "Title")
    ));
    docxtpl_pdf::docx_to_pdf(&document).expect("convert");
    let requests = docxtpl_pdf::font_requests_json();
    assert!(requests.contains("Absent Fixture Family"), "{requests}");
    // Draining them leaves the next conversion with a clean list.
    assert_eq!(docxtpl_pdf::font_requests_json(), "[]");
}

#[test]
fn fast_compression_writes_a_larger_pdf_with_the_same_markers() {
    let _fonts = FONTS
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    only_fixture_fonts();
    let body: String = (0..40)
        .map(|_| marked("MARKS0", FIXTURE_FAMILY, LONG, "MARKE0"))
        .collect();
    let document = minimal_docx(&format!("{body}{SECTION}"));
    let smallest = docxtpl_pdf::docx_to_pdf_marked(&document, side).expect("convert");
    let fast = docxtpl_pdf::docx_to_pdf_marked_with(
        &document,
        &PdfOptions::new().compression(Compression::Fast),
        side,
    )
    .expect("convert");
    assert!(fast.pdf.starts_with(b"%PDF"));
    assert!(
        fast.pdf.len() > smallest.pdf.len(),
        "{} <= {}",
        fast.pdf.len(),
        smallest.pdf.len()
    );
    assert_eq!(fast.markers, smallest.markers);
}

/// The PDF's text with every stream inflated: the converter packs its
/// annotations into compressed object streams.
fn inflated(pdf: &[u8]) -> String {
    let mut text = String::from_utf8_lossy(pdf).into_owned();
    let mut at = 0;
    while let Some(start) = pdf[at..]
        .windows(7)
        .position(|w| w == b"stream\n")
        .map(|i| at + i + 7)
    {
        let Some(end) = pdf[start..]
            .windows(10)
            .position(|w| w == b"\nendstream")
            .map(|i| start + i)
        else {
            break;
        };
        let mut data = Vec::new();
        if std::io::Read::read_to_end(
            &mut flate2::read::ZlibDecoder::new(&pdf[start..end]),
            &mut data,
        )
        .is_ok()
        {
            text.push_str(&String::from_utf8_lossy(&data));
        }
        at = end + "\nendstream".len();
    }
    text
}

#[test]
fn links_the_lines_between_two_bookmarks() {
    let _fonts = FONTS
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    only_fixture_fonts();
    // The range opens in the second paragraph and closes in the sixth.
    let lines: String = (0..3).map(|_| run(FIXTURE_FAMILY, "Middle")).collect();
    let document = minimal_docx(&format!(
        "{}{}{lines}{}{SECTION}",
        run(FIXTURE_FAMILY, "Title"),
        marked("MARKS0", FIXTURE_FAMILY, "First", "OTHER0"),
        marked("OTHER1", FIXTURE_FAMILY, "Last", "MARKE0"),
    ));
    let links = |links: Vec<RangeLink>| {
        docxtpl_pdf::docx_to_pdf_with(&document, &PdfOptions::new().range_links(links))
            .expect("convert")
    };
    let linked = inflated(&links(vec![
        RangeLink::new("MARKS0", "MARKE0", "x:section").heading_uri("x:title"),
    ]));
    // Its first line, the lines in between and its last line; it has no
    // bold heading.
    assert_eq!(linked.matches("/URI (x:section)").count(), 3, "{linked}");
    assert!(!linked.contains("x:title"));
    // Without links, or with bookmarks the document lacks, nothing changes.
    let plain = docxtpl_pdf::docx_to_pdf(&document).expect("convert");
    assert!(!inflated(&plain).contains("/URI"));
    assert_eq!(
        links(vec![RangeLink::new("MISSING", "MARKE0", "x:none")]),
        plain
    );
}

#[test]
fn failed_conversion_clears_previous_font_requests() {
    docxtpl_pdf::use_system_fonts(false);
    docxtpl_pdf::clear_fonts();
    let document = minimal_docx(&format!(
        "{}{SECTION}",
        run("Absent Fixture Family", "Title")
    ));
    docxtpl_pdf::docx_to_pdf(&document).expect("convert");
    assert!(docxtpl_pdf::docx_to_pdf(b"not a zip").is_err());
    assert_eq!(docxtpl_pdf::font_requests_json(), "[]");
}

#[test]
fn registered_fonts_and_requests_remain_thread_local() {
    let threads: Vec<_> = [true, false]
        .into_iter()
        .map(|supply| {
            std::thread::spawn(move || {
                docxtpl_pdf::use_system_fonts(false);
                docxtpl_pdf::clear_fonts();
                assert_eq!(docxtpl_pdf::register_font(b"not a font"), 0);
                if supply {
                    assert_eq!(docxtpl_pdf::register_font(FIXTURE_FONT), 1);
                }
                let document = minimal_docx(&format!("{}{SECTION}", run(FIXTURE_FAMILY, "Title")));
                docxtpl_pdf::docx_to_pdf(&document).expect("convert");
                let requests = docxtpl_pdf::font_requests();
                assert_eq!(
                    requests.iter().any(|r| r.names == [FIXTURE_FAMILY]),
                    !supply
                );
                assert!(requests.iter().all(|r| r.names.len() == 1));
                docxtpl_pdf::clear_fonts();
                docxtpl_pdf::docx_to_pdf(&document).expect("convert after clear");
                assert!(
                    docxtpl_pdf::font_requests()
                        .iter()
                        .any(|r| r.names == [FIXTURE_FAMILY])
                );
            })
        })
        .collect();
    for thread in threads {
        thread.join().unwrap();
    }
}
