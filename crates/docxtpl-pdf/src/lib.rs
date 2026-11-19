//! DOCX to PDF with a Word-compatible layout, for hosts without a file
//! system (a browser, wasm32).
//!
//! The converter never reads files, so the host supplies the fonts: it
//! registers font files ([`register_font`]), converts, asks which lookups
//! found nothing ([`font_requests`]: per lookup, the family names tried in
//! order), fetches a face for the first name it has in each, registers it and
//! converts again. Only the fonts a document uses are fetched, and the result
//! is what a full font catalog would give. Where the host has fonts of its
//! own, [`use_system_fonts`] decides whether they are used as well (on wasm32
//! there are none).
//!
//! [`docx_to_pdf_marked`] also reports where the document's bookmarks were
//! laid out, which lets a caller link a place in the PDF back to the template
//! that produced it. Only body paragraphs are reported: headers, footers and
//! text boxes are not part of this.
//!
//! # What this costs a wasm32 module
//!
//! The converter reads DOCX packages with `zip`, which depends on `getrandom`
//! on every wasm32 target whatever its features, and the backend `getrandom`
//! selects there reaches the host through wasm-bindgen. A module that links
//! this crate therefore stops being import-free: it carries four wasm-bindgen
//! placeholders (`__wbindgen_describe` and three `__wbindgen_externref_*`)
//! that a `wasm-bindgen` post-processing step would normally remove.
//!
//! Nothing here asks for randomness — `zip` wants it for the AES encryption
//! this build does not enable — so those imports are never called, and the
//! host can satisfy them with functions that fail loudly if they ever are.
//! The `@docxtpl/wasm` package in `js/` exports exactly that, as
//! `bindgenPlaceholders`. Selecting `getrandom`'s custom backend instead does
//! not currently work: getrandom 0.4.3 names an error constant under its
//! `wasm_js` feature that only exists when the `wasm_js` backend is the one
//! compiled, so the two cannot be separated from the outside.

pub use docxide_pdf::{Compression, FontRequest};

/// Settings of one conversion. The defaults are the converter's own (on a
/// host with an environment, what its `DOCXIDE_*` variables select).
#[derive(Debug, Clone, Default)]
#[non_exhaustive]
pub struct PdfOptions {
    /// How hard the PDF's streams are compressed. [`Compression::Fast`]
    /// writes the same PDF several times faster as a larger file: for a PDF
    /// shown where it is made, such as a live preview.
    pub compression: Option<Compression>,
}

impl PdfOptions {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn compression(mut self, compression: Compression) -> Self {
        self.compression = Some(compression);
        self
    }

    fn converter(&self) -> docxide_pdf::ConvertOptions {
        let options = docxide_pdf::ConvertOptions::from_env();
        match self.compression {
            Some(compression) => options.compression(compression),
            None => options,
        }
    }
}

/// Which end of a bookmarked paragraph a marker stands for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MarkerSide {
    /// The top of the paragraph's first line.
    Start,
    /// The bottom of the paragraph's last line.
    End,
}

/// Where a marker was laid out: page index (0-based), `x` in points from the
/// left edge (0 for a line start, a large value for a line end) and `y` in
/// points from the bottom of the page (PDF space; a viewer turns it top-down).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MarkerPosition {
    pub page: usize,
    pub x: f64,
    pub y_from_bottom: f64,
}

/// A PDF and the positions of the markers found in it.
#[derive(Debug, Clone, Default)]
pub struct MarkedPdf {
    pub pdf: Vec<u8>,
    /// Marker name → position, in the order the converter reported them.
    /// Markers that could not be placed are absent.
    pub markers: Vec<(String, MarkerPosition)>,
}

/// An `x` past any line's end.
const LINE_END: f64 = 1.0e6;
/// How far inside a paragraph's last line an end marker is put (points above
/// the line's bottom), so it falls on that line.
const INSIDE_LAST_LINE: f64 = 1.0;

/// Lay out `docx` and render it to PDF bytes.
pub fn docx_to_pdf(docx: &[u8]) -> Result<Vec<u8>, String> {
    docx_to_pdf_with(docx, &PdfOptions::default())
}

/// [`docx_to_pdf`] with `options`.
pub fn docx_to_pdf_with(docx: &[u8], options: &PdfOptions) -> Result<Vec<u8>, String> {
    docxide_pdf::convert_docx_bytes_to_pdf_bytes_with(docx, &options.converter())
        .map_err(|error| format!("render PDF: {error}"))
}

/// Convert `docx` and report where its markers were laid out. `side` names a
/// bookmark's marker side, or returns `None` for a bookmark that is not a
/// marker.
pub fn docx_to_pdf_marked(docx: &[u8], side: impl Fn(&str) -> Option<MarkerSide>) -> Result<MarkedPdf, String> {
    docx_to_pdf_marked_with(docx, &PdfOptions::default(), side)
}

/// [`docx_to_pdf_marked`] with `options`.
pub fn docx_to_pdf_marked_with(
    docx: &[u8],
    options: &PdfOptions,
    side: impl Fn(&str) -> Option<MarkerSide>,
) -> Result<MarkedPdf, String> {
    let pdf = docx_to_pdf_with(docx, options)?;
    let mut markers = Vec::new();
    for bookmark in docxide_pdf::take_bookmark_positions() {
        let position = match side(&bookmark.name) {
            None => continue,
            Some(MarkerSide::End) => MarkerPosition {
                page: bookmark.end_page,
                x: LINE_END,
                y_from_bottom: f64::from(bookmark.bottom) + INSIDE_LAST_LINE,
            },
            Some(MarkerSide::Start) => MarkerPosition {
                page: bookmark.page,
                x: 0.0,
                y_from_bottom: f64::from(bookmark.top),
            },
        };
        markers.push((bookmark.name, position));
    }
    Ok(MarkedPdf { pdf, markers })
}

impl MarkedPdf {
    /// The marker positions as JSON: `{name: {page, x, y_from_bottom}}`, for
    /// a host that reads them next to the PDF bytes.
    pub fn markers_json(&self) -> serde_json::Value {
        let mut markers = serde_json::Map::new();
        for (name, position) in &self.markers {
            markers.insert(name.clone(), serde_json::json!({
                "page": position.page, "x": position.x, "y_from_bottom": position.y_from_bottom,
            }));
        }
        serde_json::Value::Object(markers)
    }
}

/// Register a font file (every face of a collection) for later conversions.
/// Returns the number of faces registered (0: not a font).
pub fn register_font(font: &[u8]) -> usize {
    docxide_pdf::register_font(font.to_vec())
}

/// Forget every registered font.
pub fn clear_fonts() {
    docxide_pdf::clear_fonts();
}

/// Whether the host's own fonts are used as well (on wasm32 there are none).
pub fn use_system_fonts(enabled: bool) {
    docxide_pdf::use_system_fonts(enabled);
}

/// The font lookups that found nothing in the last conversion, with the
/// family names tried in order.
pub fn font_requests() -> Vec<FontRequest> {
    docxide_pdf::take_font_requests()
}

/// [`font_requests`] as JSON: `[{"names": [family, ...], "bold": bool,
/// "italic": bool}]`, for a host that fetches the faces itself.
pub fn font_requests_json() -> String {
    let requests: Vec<serde_json::Value> = font_requests()
        .into_iter()
        .map(|request| serde_json::json!({"names": request.names, "bold": request.bold, "italic": request.italic}))
        .collect();
    serde_json::Value::Array(requests).to_string()
}
