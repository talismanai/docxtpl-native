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

/// A `width`×`height` PNG of one flat colour, where Word's export keeps the
/// lossless (Flate) copy.
fn flat_png(width: u32, height: u32) -> Vec<u8> {
    fn chunk(out: &mut Vec<u8>, kind: &[u8], data: &[u8]) {
        let mut crc = flate2::Crc::new();
        crc.update(kind);
        crc.update(data);
        out.extend((data.len() as u32).to_be_bytes());
        out.extend(kind);
        out.extend(data);
        out.extend(crc.sum().to_be_bytes());
    }
    let mut header = Vec::new();
    header.extend(width.to_be_bytes());
    header.extend(height.to_be_bytes());
    header.extend([8, 2, 0, 0, 0]);
    let row: Vec<u8> = std::iter::once(0).chain((0..width).flat_map(|_| [40, 90, 160])).collect();
    let mut encoder = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::default());
    for _ in 0..height {
        std::io::Write::write_all(&mut encoder, &row).unwrap();
    }
    let mut png = b"\x89PNG\r\n\x1a\n".to_vec();
    chunk(&mut png, b"IHDR", &header);
    chunk(&mut png, b"IDAT", &encoder.finish().unwrap());
    chunk(&mut png, b"IEND", &[]);
    png
}

/// A letter page with `picture` drawn 100×50pt.
fn picture_document(picture: &[u8]) -> Vec<u8> {
    const IMAGE: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image";
    let drawing = r#"<w:p><w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"><wp:extent cx="1270000" cy="635000"/><wp:docPr id="1" name="p"/><a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:nvPicPr><pic:cNvPr id="1" name="p"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:embed="RID"/></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1270000" cy="635000"/></a:xfrm></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"#;
    let mut package =
        docxtplrs::package::Package::from_bytes(&minimal_docx(SECTION)).expect("package");
    let mut rels = (*package.rels("word/document.xml")).clone();
    let id = rels.add(IMAGE, "media/image1.png", false);
    package.save_rels("word/document.xml", &rels);
    package.set("word/media/image1.png", picture.to_vec());
    let types = package.get_string("[Content_Types].xml").expect("content types").replace(
        "</Types>",
        r#"<Default Extension="png" ContentType="image/png"/></Types>"#,
    );
    package.set("[Content_Types].xml", types.into_bytes());
    let body = package.get_string("word/document.xml").expect("body");
    let body = body.replacen("<w:sectPr", &format!("{}<w:sectPr", drawing.replace("RID", &id)), 1);
    package.set("word/document.xml", body.into_bytes());
    package.to_bytes().expect("docx")
}

#[test]
fn picture_options_reach_the_converter() {
    let _fonts = FONTS
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    only_fixture_fonts();
    // 1600×800 drawn at 100×50pt: 1152 ppi.
    let document = picture_document(&flat_png(1600, 800));
    let convert = |options: &PdfOptions| {
        docxtpl_pdf::docx_to_pdf_marked_with(&document, options, side)
            .expect("convert")
            .pdf
    };
    let word = inflated(&convert(&PdfOptions::new()));
    assert!(word.contains("/Width 267") && word.contains("/FlateDecode"), "192 ppi, lossless");
    let draft = PdfOptions::new().picture_resolution(144.0).draft_pictures(true);
    let drafted = convert(&draft);
    let text = inflated(&drafted);
    assert!(text.contains("/Width 200") && text.contains("/DCTDecode"), "144 ppi JPEG");
    // The cache hands back the same picture: the same PDF.
    let cached = draft.picture_cache(true);
    assert_eq!(convert(&cached), drafted);
    assert_eq!(convert(&cached), drafted);
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
