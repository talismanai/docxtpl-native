# docxtpl-native

A Rust engine for [python-docx-template](https://github.com/elapouya/python-docx-template)
(docxtpl) templates: Word documents with Jinja2 tags, rendered without Python.
It runs as a Rust library, as a WebAssembly module in a browser, and behind a
Python binding that keeps the docxtpl API. A differential laboratory compares
every output with docxtpl itself.

**Status: experimental.** Read [Status and limits](#status-and-limits) before
you depend on it.

## What it is for

docxtpl needs a Python process. This engine renders the same templates where
there is none:

- **In the browser.** A page that fills a form can render the document on
  every edit and show the real result, with no round trip to a server. The
  server keeps producing the final file with docxtpl (or with this engine), and
  the laboratory checks whether both give the same document for the supported
  template features and context.
- **In Rust programs.** Services, command-line tools and workers render
  templates as a library call.
- **In Python, with less Python.** The binding has its own `DocxTemplate`,
  `RichText`, `InlineImage` and the other docxtpl classes, with the same
  signatures, and moves the algorithms to Rust. It does not need docxtpl.

The goal is not "a templating engine for DOCX". It is the same output as
docxtpl: the same XML, the same package parts and, where the laboratory can
prove it, the same bytes.

## Three ways in

### Rust

The engine crates are not on crates.io yet. Depend on a commit, and select the
audited forks of MiniJinja and zlib-rs that the engine renders with (Cargo does
not inherit patches from a dependency's workspace):

```toml
[dependencies]
# One dependency for every layer you use; `docxtpl` re-exports them, and the
# MiniJinja your template values belong to, so nothing can drift apart.
docxtpl = { git = "https://github.com/talismanai/docxtpl-native", tag = "v<version>", features = ["wasm"] }

[patch.crates-io]
minijinja = { git = "https://github.com/talismanai/docxtpl-native", tag = "v<version>" }
zlib-rs = { git = "https://github.com/talismanai/docxtpl-native", tag = "v<version>" }
```

The features are `pycompat`, `wasm`, `markers`, `pdf`, `host-core`, and
`browser` for the four a browser pipeline uses. A single crate also works
(`docxtpl-wasm = { git = ..., tag = ... }`); the facade exists so that one
release tag is written once instead of once per crate. Pin a release tag
(`v<version>`, see [Releases](CONTRIBUTING.md#releases)), not a branch: the
three entries must name the same tag.

```rust
let template = std::fs::read("template.docx")?;
let document = docxtpl_wasm::render(&template, br#"{"name": "World"}"#, true)?;
std::fs::write("result.docx", document)?;
```

`docxtpl_wasm::document::render_document` takes a prepared context
(`value::Ctx`) with pictures, rich text and subdocuments, and lets you add
filters and globals. Despite its name, `docxtpl-wasm` is the embedding API for
every target, not only for WebAssembly.

### WebAssembly

```bash
rustup target add wasm32-unknown-unknown
cargo build -p docxtpl-wasm --target wasm32-unknown-unknown --profile wasm --locked
# target/wasm32-unknown-unknown/wasm/docxtpl_wasm.wasm
```

The module has no imports, which CI checks on every build
(`scripts/check_wasm_imports.mjs`), and a small C ABI: allocate the inputs,
call an export, read the output. A module that also links `docxtpl-pdf`
imports four wasm-bindgen placeholders that are never called;
[js/README.md](js/README.md) shows how a host supplies them.

```js
const { instance } = await WebAssembly.instantiate(wasmBytes, {});
const api = instance.exports;

function put(bytes) {
  const pointer = api.docxtpl_alloc(bytes.length);
  new Uint8Array(api.memory.buffer, pointer, bytes.length).set(bytes);
  return [pointer, bytes.length];
}

const [template, templateLength] = put(templateBytes);
const [context, contextLength] = put(new TextEncoder().encode(JSON.stringify({ name: "World" })));
const status = api.docxtpl_render(template, templateLength, context, contextLength, 1);
// Copy the output before the next call: it is the DOCX (status 0) or a UTF-8 error message (status 1).
const output = new Uint8Array(api.memory.buffer, api.docxtpl_output_ptr(), api.docxtpl_output_len()).slice();
api.docxtpl_free(template, templateLength);
api.docxtpl_free(context, contextLength);
```

Run it in a Web Worker to keep the page responsive. The `wasm` profile builds
for download size; CI reports the raw and compressed size of every build in
its job summary. A crate that embeds the engine adds its own exports with
`docxtpl_wasm::export_bytes!` and the helpers in `docxtpl_wasm::abi`.

### Python

Needs Rust 1.96.0 (`rust-toolchain.toml` selects it), Python 3.11 or 3.12,
pkg-config, libxml2 and libclang headers.

```bash
uv venv
uv pip install 'maturin>=1.9,<2'
uv run maturin develop --release --locked
```

```python
from docxtpl_native import DocxTemplate

doc = DocxTemplate("template.docx")
doc.render({"name": "World"})
doc.save("result.docx")
```

The classes are the package's own: install and run it without docxtpl. Code
written for docxtpl works after changing the import, with the differences
listed in [docs/API.md](docs/API.md). Subdocuments need the `subdoc` extra
(`docxtpl-native[subdoc]`), as they need `docxtpl[subdoc]`.

A feature the engine does not support raises an explicit error. The binding
never falls back to docxtpl or to a Jinja2 render.

## Status and limits

| Front | Where it stands |
| --- | --- |
| Browser and Rust engine | Renders DOCX without Python. Jinja2 builtins match Jinja2 on every case of the conformance check, on the host and in the `.wasm`. Known gaps: `tuple + tuple` gives a list, `{% break %}` works without the `loopcontrols` extension, and error messages differ from Jinja2. |
| Python binding | Passes the whole differential corpus with required visual comparison, apart from the registered divergences, on Linux arm64 and x86_64 with CPython 3.11 and 3.12. Every catalogued partition is bound to a probe. A few paths still run Python code; they are listed in the status document. |
| Performance | Depends on the workload. Plain text and long documents are faster; lookups, includes, pictures and subdocuments are slower. There is no basis to promise a general speedup. |
| Not validated | Rendering in Microsoft Word, documents outside the generated corpus, and platforms other than the four declared targets. |
| Deliberate differences | In three places the output differs from the reference on purpose, because reproducing it would damage the document. Each one, with its reason, is in the status document; tests pin both results. |

The state of each area, the decisions behind it, the accepted debts and what
is still missing are in [docs/STATUS.md](docs/STATUS.md), with the commands
that check them.

## Architecture at a glance

```text
            template.docx + context
                      |
   +------------------v-------------------+
   |  Engine: Rust only, no Python, no C  |
   |  docxtpl           one dependency that re-exports the layers below
   |  docxtpl-wasm      render API, Jinja2 builtins, WebAssembly ABI
   |  docxtpl-pycompat  lxml, python-docx and Pillow behaviours for pipelines
   |  docxtpl-markers   link a rendered document back to its template
   |  docxtpl-pdf       DOCX to PDF, fonts supplied by the host
   |  docxtplrs (fork)  DOCX package, tag patching, composition
   |  minijinja (fork) + rsyi/jinja      template VM with Jinja2 semantics
   |  zlib-rs (fork)    deflate with the byte output of CPython's zlib
   +------------------+-------------------+
                      |
   +------------------v-------------------+
   |  Host core: Rust + libxml2           |
   |  docxtpl-core      OPC writer and XML policies of python-docx and lxml
   +------------------+-------------------+
                      |
   +------------------v-------------------+
   |  Python binding (opt-in)             |
   |  bindings/python   PyO3 extension: Python objects, callbacks, async
   |  python/docxtpl_native               the Python package (own classes, no docxtpl)
   +--------------------------------------+

   Parity laboratory: python/paritylab, tests/, scripts/, compatibility/
   Runs docxtpl and the engine on the same cases and compares the documents.
```

| Path | What lives there |
| --- | --- |
| `crates/docxtpl` | The facade: one dependency, one revision, a feature per layer |
| `crates/docxtpl-wasm` | Render API for embedders, Jinja2 builtins, the WebAssembly ABI |
| `crates/docxtpl-pycompat` | Behaviours of Python libraries that compatible pipelines need: lxml trees, python-docx relationships, pictures as Pillow reads them, `str`/`repr`, `re` templates |
| `crates/docxtpl-markers` | In-line markers moved to paragraph anchors, so a caller can link a place in the output back to the template without changing the layout; markers the render lost placed again by counting back from another |
| `crates/docxtpl-pdf` | DOCX to PDF with a Word-compatible layout, for a host with no file system: fonts registered in memory, on demand, the page positions of the document's bookmarks, and link annotations over the text between two bookmarks |
| `crates/docxtpl-core` | Host core shared by language bindings: OPC writer, libxml2 policies |
| `vendor/` | Audited forks (`docxtplrs`, `minijinja`, `zlib-rs`) with provenance manifests |
| `bindings/python` | The PyO3 extension crate |
| `python/` | `docxtpl_native` (the Python package) and `paritylab` (the laboratory) |
| `js/` | `@docxtpl/wasm`: the host side of the WebAssembly ABI for a browser or Node, with no dependencies |
| `tests/`, `scripts/`, `compatibility/` | Differential tests, tools and the contract catalog |

`cargo build` and `cargo test` at the root build the engine and never touch
Python. The binding is a workspace member you select with
`-p docxtpl-native` or build with maturin.
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) has the details.

## Built on and inspired by

**The reference.** These define what "correct" means. The engine reimplements
their behaviour, and the laboratory runs them as the oracle.

- [python-docx-template](https://github.com/elapouya/python-docx-template)
  (docxtpl 0.20.2): the template model, the tag rules and the API.
- [Jinja2](https://github.com/pallets/jinja) 3.1.6 and
  [MarkupSafe](https://github.com/pallets/markupsafe): the template language,
  its builtins and its escaping.
- [python-docx](https://github.com/python-openxml/python-docx) 1.2.0: how a
  package is read and written (part order, content types, relationships).
- [docxcompose](https://github.com/4teamwork/docxcompose) 1.4.0: how
  subdocuments are merged (styles, numbering, bookmarks).
- [lxml](https://lxml.de/) 6 and [libxml2](https://gitlab.gnome.org/GNOME/libxml2):
  XML parsing, recovery and serialization.
- [Pillow](https://python-pillow.org/): how pictures are identified, measured
  and rotated.
- CPython: `str`/`repr`, the `zlib` output and other details that leak into
  rendered documents.

**The building blocks.** The Rust code the engine stands on.

- [docxtplrs](https://github.com/yiyinzhang/docxtplrs): a Rust port of docxtpl.
  It is the base of the DOCX package handling, the tag patching and the
  composition. Vendored as a fork.
- [MiniJinja](https://github.com/mitsuhiko/minijinja): the template VM.
  Vendored as a fork.
- [rsyi/jinja](https://github.com/rsyi/jinja): a Jinja2 compatibility layer
  over MiniJinja.
- [zlib-rs](https://github.com/trifectatechfoundation/zlib-rs): deflate in
  Rust. Vendored as a fork.
- [rust-libxml](https://github.com/KWARC/rust-libxml): libxml2 from Rust, used
  by the host core and the binding.
- [image](https://github.com/image-rs/image), `png`, `zune-jpeg` and `tiff`:
  picture decoding behind the Pillow-compatible layer.
- [PyO3](https://github.com/PyO3/pyo3) and
  [maturin](https://github.com/PyO3/maturin): the Python binding.
  [corosensei](https://github.com/Amanieu/corosensei) drives its async path.

Why the engine is assembled this way, what it reuses, what it forks and why,
is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#why-it-is-built-this-way).

Licenses and pinned revisions are in [THIRD_PARTY.md](THIRD_PARTY.md).

## Develop

```bash
cargo test --release --locked            # the engine, no Python involved
scripts/check_engine_boundary.sh         # the engine stays free of Python
cargo build -p docxtpl-wasm --target wasm32-unknown-unknown --profile wasm --locked
```

`docxtpl-core` links libxml2, so install its headers and libclang first (on
Debian or Ubuntu: `libxml2-dev libclang-dev pkg-config`).
[CONTRIBUTING.md](CONTRIBUTING.md) covers the Python binding, the parity
laboratory, the fork rules and releases.

## Documentation

[docs/README.md](docs/README.md) lists every document: the API, the
architecture, the compatibility contract and the status.

## License

LGPL-2.1-or-later, the license of docxtplrs and close to that of docxtpl
(LGPL-2.1-only). See [LICENSE](LICENSE) and [THIRD_PARTY.md](THIRD_PARTY.md).
