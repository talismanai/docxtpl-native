# Changelog

Notable changes per release. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/); before 1.0, a minor version may
break the API or change rendered output.

The section of a version becomes the opening of its GitHub release notes, so
write it for someone deciding whether to upgrade.

## [Unreleased]

### Fixed

- `docxtpl-wasm`: rich text in a prepared context (`Ctx::Rich`) reaches the
  template as a `RichText` object, as docxtpl's does, instead of a markup
  string. `is string` is now false, `xml` holds the run XML, an empty one is
  true, and `|e` keeps it markup. Templates that read a value's text through
  `value.xml` when it is not a string now take that branch and compare equal
  where they did not. Without autoescape, printing it, `{{r }}`, `~`,
  `|string`, `|safe` and the string filters give the same output as before;
  with autoescape, `~`, `|string` and the string filters now give plain text,
  escaped on output, as Jinja2 does.
- `docxtpl-wasm`: with autoescape, `join` returns markup when the separator or
  an item is markup, escaping the other pieces, as Jinja2 does. It used to
  return a plain string, escaped again on output.
- `docxtpl-wasm`: the `strip`, `lstrip` and `rstrip` string methods take
  Python's `chars` argument (`'a, '.rstrip(' ,')` is `'a'`) and, without it,
  also strip U+001C to U+001F as `str.isspace` does. The argument used to be
  ignored and only whitespace was stripped.

## [0.3.4] - 2026-11-28

### Added

- `docxtpl-pdf`: `PdfOptions::picture_resolution`, `draft_pictures` and
  `picture_cache`, for a PDF shown where it is made (a live preview that
  converts again after every edit): resample pictures to a lower resolution,
  resample them cheaply and write them as JPEG, and reuse the pictures earlier
  conversions in the process encoded with the same settings (up to 64 MiB).
  Off by default: without them the output does not change.

### Changed

- Update the PDF converter to docxide-pdf `v0.22.0-talisman.5`, on upstream
  `fe2cd29fb`. `Compression::Fast` now also changes pictures: JPEGs that need
  no change are embedded as stored, other pictures are written as Flate
  without a JPEG trial, and only pictures above 384 ppi are resampled, with
  the thumbnail filter. That is faster and can be several times larger with
  screenshots; `draft_pictures` keeps them small. The default compression
  writes the same PDF as before.

### Fixed

- `docxtpl-wasm`: the `string` filter turns a picture into its run XML, as
  `str(InlineImage)` does under docxtpl. It returned the picture's internal
  placeholder, so a template that tests or edits a picture's markup
  (`'pic:pic' in picture|string`, `picture|string|replace(...)`) dropped the
  picture or left it unchanged in `render_document`, while docxtpl kept or
  changed it.

## [0.3.3] - 2026-11-27

### Fixed

- Update the PDF converter to docxide-pdf `v0.22.0-talisman.4`: a floating
  picture now keeps its own range-link rectangle. In 0.3.2, contrary to its
  notes, a floating picture laid out between a range's first and last lines
  was merged into the middle rectangle, which could then cover content
  outside the range.

## [0.3.2] - 2026-11-27

### Changed

- Update the PDF converter to docxide-pdf `v0.22.0-talisman.3`: range links
  now cover floating pictures anchored in a range (body, table cells, VML) and
  a picture alone in a text box, each floating picture with its own rectangle.

## [0.3.1] - 2026-11-25

### Changed

- Update the PDF converter to docxide-pdf `v0.22.0-talisman.2`.
- Copy only the requested XML subtree when duplicating a DOM node, reducing
  allocation work for templates with repeated formatting copies.
- Stop searching descendants as soon as a matching element is found.

## [0.3.0] - 2026-11-23

### Changed

- **PDF converter** (`docxtpl-pdf`): docxide-pdf `v0.22.0-talisman.1`, based
  on upstream 0.22 with the fork's compression, layout optimizations and
  bookmark range links. The adapter uses `convert()` and its returned PDF,
  bookmarks and missing fonts. Font registration remains thread-local.
- **Font requests** keep the `{names, bold, italic}` JSON shape, but `names`
  now contains one selected document family. The converter no longer exposes
  its fallback probes for glyph rescue; on-demand loading does not guarantee
  the result of preloading a complete fallback catalog. A failed conversion
  clears pending requests. Library conversions no longer read `DOCXIDE_*`.

## [0.2.2] - 2026-11-19

### Added

- **Range links** (`docxtpl-pdf`): `PdfOptions::range_links` takes
  `RangeLink`s (start and end bookmark names, a URI and an optional heading
  URI) and the converter writes invisible link annotations over the text and
  inline pictures laid out between the two bookmarks — whether they sit
  between paragraphs or between the runs of a line — with at most three
  rectangles per range and page (first line, the lines between, last line),
  plus a link over the range's leading bold lines when a heading URI is given.
  A caller that links a place in the PDF back to its template no longer has
  to find the text in the PDF itself. Without range links the PDF is
  unchanged.
- **Relative markers as bookmarks** (`docxtpl-markers`):
  `place_relative_markers` inserts a marker the render lost as a point
  bookmark of its own, a number of characters before the marker it counts back
  from (splitting the run there), for a converter that places bookmarks inside
  a line itself, where moving markers to paragraph anchors is not needed.

## [0.2.1] - 2026-11-19

### Security

- **quick-xml 0.42**, for RUSTSEC-2026-0194 (quadratic duplicate-attribute
  check) and RUSTSEC-2026-0195 (unbounded namespace declarations). Text and
  attribute values with entity and character references parse as before.
  Namespace-aware XML readers now reject more
  than 128 simultaneous namespace bindings, including inherited declarations;
  this security limit is retained rather than disabled.

## [0.2.0] - 2026-11-19

### Added

- **PDF options** (`docxtpl-pdf`): `docx_to_pdf_with` and
  `docx_to_pdf_marked_with` take `PdfOptions`. `compression(Compression::Fast)`
  writes the same PDF several times faster as a larger file, for a PDF shown
  where it is made, such as a live preview converted again on every edit.

### Changed

- **PDF converter** (`docxtpl-pdf`): docxide-pdf `v0.21.4-talisman.2`, which
  lays out and draws text about twice as fast with byte-identical output, and
  adds the compression option above.

## [0.1.0] - 2026-11-19

The first release. Experimental: read [Status and limits](README.md#status-and-limits)
before depending on it.

### Added

- **Rust engine** (`docxtpl-wasm`): renders docxtpl templates without Python,
  as a library and as a WebAssembly module with no imports. Contexts carry
  pictures, rich text and subdocuments; an embedder adds filters, globals and
  its own exports (`export_bytes!`, `abi::envelope`).
- **Jinja2 builtins with Python semantics**: filters, tests, globals,
  `str`/`list`/`dict` methods, `%` and `str.format`.
- **Python behaviours without Python** (`docxtpl-pycompat`): lxml tree
  semantics, python-docx relationships, pictures as Pillow reads and rotates
  them (PNG, JPEG, GIF, BMP, WebP, TIFF), `str`, `repr` and `str.isspace`, and
  `re` replacement templates.
- **Markers** (`docxtpl-markers`): bookmarks inside a line moved to anchors at
  the start of their paragraph, with the offset that finds the spot again, so
  a document that links back to its template is laid out like one that does
  not.
- **PDF** (`docxtpl-pdf`): DOCX to PDF with a Word-compatible layout for a host
  with no file system, with fonts registered in memory and fetched on demand,
  and the page positions of the document's bookmarks.
- **One dependency** (`docxtpl`): the engine and its layers behind one crate
  and one revision, a feature per layer, with the MiniJinja that template
  values belong to.
- **Host core** (`docxtpl-core`): the package writer of python-docx 1.2.0 and
  the libxml2 policies shared by language bindings.
- **Python binding** (`docxtpl_native`): the `DocxTemplate` API of docxtpl
  0.20.2 with the package's own classes, so docxtpl is not a dependency.
  Native access to the document's package (`open_package`,
  `commit_package`), custom Jinja2 delimiters, a native parser for a closed
  template grammar, a package written in Rust on save, and
  `python -m docxtpl_native`. Subdocuments need the `subdoc` extra. An
  unsupported feature raises `UnsupportedFeature`; nothing falls back to
  docxtpl.
- **Host side of the ABI** (`js/`, `@docxtpl/wasm`): load a module, call its
  exports, read envelopes and answer its font lookups, with no dependencies.
- **Differential laboratory** (`paritylab`): compares documents and
  render-time behaviour with the pinned Python reference.
- **Audited forks** of docxtplrs, MiniJinja and zlib-rs, with provenance
  manifests that `scripts/check_vendor.py` verifies.

### Known limits

- Three deliberate differences from docxtpl's output, and differences that
  only code inspecting CPython or Jinja2's generated Python can observe; both
  are listed with their reasons in [docs/STATUS.md](docs/STATUS.md).
- Some paths still run Python code ([docs/STATUS.md](docs/STATUS.md#what-still-runs-python)).
- `tuple + tuple` gives a list, `{% break %}` works without the `loopcontrols`
  extension, and error messages differ from Jinja2.
- Not validated in Microsoft Word or on documents outside the generated
  corpus.
- `docxtpl-pdf` needs Rust 1.96 (the rest of the engine needs 1.93), and a
  WebAssembly module that links it imports four wasm-bindgen placeholders the
  host has to supply.
