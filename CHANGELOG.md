# Changelog

Notable changes per release. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/); before 1.0, a minor version may
break the API or change rendered output.

The section of a version becomes the opening of its GitHub release notes, so
write it for someone deciding whether to upgrade.

## [Unreleased]

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
