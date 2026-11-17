# Architecture

The repository has three layers and one laboratory. Each layer may depend only
on the ones above it in this list.

| Layer | Where | May use |
| --- | --- | --- |
| Engine | `crates/docxtpl`, `crates/docxtpl-wasm`, `crates/docxtpl-pycompat`, `crates/docxtpl-markers`, `crates/docxtpl-pdf`, `vendor/docxtplrs`, `vendor/minijinja`, `vendor/zlib-rs` | Rust only. No Python, no C. Builds for `wasm32-unknown-unknown` |
| Host core | `crates/docxtpl-core` | Rust and C libraries called directly from Rust (libxml2). No Python |
| Python binding | `bindings/python` (PyO3 crate), `python/docxtpl_native` | Everything above, plus CPython |
| Laboratory | `python/paritylab`, `tests/`, `scripts/`, `compatibility/` | The binding and the pinned Python reference |

`scripts/check_engine_boundary.sh` enforces the first two rows in CI: it fails
when an engine crate or the host core gains a Python dependency, when a C
deflate backend replaces the audited compressor, or when the browser path
links any C library.

## Why it is built this way

- **Build on a port.** The engine builds on docxtplrs, a Rust port of docxtpl
  over MiniJinja, and adds hooks where docxtpl behaves differently. The
  template model and the tag rules are already encoded there, so new code goes
  where compatibility needs it.
- **Fork only where compatibility needs it.** MiniJinja (Python lookup and
  truthiness rules, loop and import semantics), docxtplrs (package, picture
  and subdocument contracts, XML recovery) and zlib-rs (the level-6 strategy
  of CPython's zlib, so archives come out byte for byte) are vendored; every
  other dependency is used as published. Each fork records its upstream
  commit, the hash of every imported file and every local change, so upstream
  code stays distinguishable from ours and `scripts/check_vendor.py` rejects a
  change nobody recorded.
- **Write only what no library provides:** the Jinja2 builtins with Python
  semantics, the OPC writer that reproduces python-docx, the lxml, python-docx
  and Pillow behaviours in `docxtpl-pycompat`, the WebAssembly ABI and the
  Python binding.
- **The reference decides what is correct.** The pinned Python stack is the
  oracle, and [the laboratory](#the-laboratory) compares against it; no
  behaviour is correct because this code does it.

## The engine

A render goes through these steps, all in Rust:

1. **Open the package.** `docxtplrs::package` reads the DOCX (a ZIP of XML
   parts and media). Media stay compressed until something needs them.
2. **Patch the tags.** Word splits `{{ name }}` across runs and wraps tags in
   markup. `docxtplrs::patch` rebuilds clean Jinja source from each part, with
   the rules of docxtpl (paragraph, row and run tags, `colspan`, `cellbg`,
   `-%}` and the others).
3. **Render the template.** The MiniJinja fork runs the source with the
   compatibility layer of `rsyi/jinja`. `docxtpl-wasm` adds the Jinja2 builtins
   with Python semantics: filters, tests, globals, `str`/`list`/`dict` methods,
   `%` and `str.format`.
4. **Resolve rich values.** Pictures, rich text and subdocuments are deferred
   objects. They become XML, relationships and media parts after the part that
   uses them renders.
5. **Repair and compose.** `docxtplrs::recover` fixes malformed XML the way
   libxml2 recovers it, then tables are fixed and subdocuments are merged
   (styles, numbering, bookmarks).
6. **Write the package.** Parts are written back and compressed with the
   zlib-rs fork, whose level-6 output matches CPython's `zlib`.

`docxtpl-wasm` is the entry point for embedders on every target:

- `render(template, context, autoescape)` takes a JSON context.
- `document::render_document` takes a prepared context (`value::Ctx`) with
  pictures, rich text and subdocuments, and a closure that adds filters and
  globals. `DocumentOptions` selects python-docx numbering compatibility.
- `abi` is the WebAssembly ABI: `docxtpl_alloc`, `docxtpl_free`,
  `docxtpl_render`, `docxtpl_render_string`, `docxtpl_output_ptr` and
  `docxtpl_output_len`. A crate that embeds the engine adds its own exports and
  reports results with `abi::finish`.

`docxtpl-pycompat` is a toolbox for pipelines that post-process documents the
way Python code does: an XML tree with lxml semantics (`dom`), python-docx
relationships (`rels`), pictures as Pillow reads and rotates them (`images`),
Python `str`/`repr` (`pyvalue`) and `re` replacement templates (`re`). The
engine does not depend on it.

## The host core

`docxtpl-core` serves language bindings that run next to a real libxml2. It
holds the OPC writer that reproduces python-docx 1.2.0 (`opc`, see [The OPC
writer](#the-opc-writer)), image stream handling, the transport for Unicode code
points that do not fit in a Rust string, and `foreign_xml`, which serializes a
tree owned by another libxml2 instance loaded at run time (the one a host
extension bundles). It has no PyO3 dependency. It is not part of the browser
path.

Its interface is internal (`publish = false`). It exposes some `docxtplrs`
types, and its OPC code consumes `docxtplrs::package::Package`. Do not present
it as a standalone OPC crate until that coupling and the full OPC contract are
addressed.

## The OPC writer

`crates/docxtpl-core/src/opc.rs` reproduces the package-writer rules of the
pinned python-docx 1.2.0, derived from its `opc/pkgreader.py`,
`opc/pkgwriter.py`, `opc/package.py`, `opc/rel.py`, `opc/packuri.py` and
`opc/spec.py` (the upstream MIT notice is kept under `vendor/notices`):

- Walk internal relationships depth first, visit a cycle once, keep only
  reachable parts and leave opaque payloads byte-identical.
- Read content types by namespace URI and case-insensitive keys, and rebuild
  the Default/Override index with the reference's exact standard-type table.
- Rebuild relationship streams, omit empty per-part streams, normalize internal
  POSIX targets and keep external targets verbatim. Namespace aliases,
  redundant path components and an explicit `Internal` mode are accepted.
- Write content types, root relationships and parts in the reference's
  traversal order; ZIP replacements run afterwards and stay verbatim.

The writer works on an `Arc`-shared clone of the package: it never mutates the
live document graph and does not inflate unchanged media while walking
relationships. `Package::to_bytes_opc` deflates every part at level 6 with the
audited zlib-rs fork; the native ZIP writer owns CRC, local headers and
payloads, and `python_zip` matches the central metadata, ZIP64 thresholds and
end records of CPython's `zipfile`. Input deflate streams are deliberately not
reused: their bytes can differ for identical payloads.

## The Python binding

`bindings/python` is the PyO3 extension module `docxtpl_native._native`.
`python/docxtpl_native` is the package users import. The binding owns Python
types, callbacks and object protocols, and calls the layers above; [The native
bridge](#the-native-bridge) describes how.

The package's classes (`DocxTemplate`, `RichText`, `RichTextParagraph`,
`Listing`, `InlineImage` in `inline_image.py`, `Subdoc`) are its own and
delegate to the extension; [API.md](API.md) is their contract. The package
and the extension never import docxtpl. The extension recognises package
values by the classes saved at import (`_OriginalInlineImage`,
`_OriginalSubdoc`, ...) and their saved methods, so it can insert a value
without calling its Python methods only while those are unchanged. Python
dependencies: python-docx, Jinja2 and lxml; docxcompose for subdocuments.
`scripts/run_product_tests.py` and `tests/test_reference_free.py` check that
no product path imports docxtpl.

The target is: algorithms in Rust, Python only at the edge. Calling an
original Python algorithm from the binding does not count as a native port.

The data-only frontend recognizes a closed grammar of paths and conditional
statements with the Rust parser. A customized Python parser supplies an AST to
the existing Rust compiler. Root symbol preparation creates the Python `self`
reference once per activation; a VM hook resolves an overridden self-block
lookup before evaluating arguments. Default block execution stays in the VM.

Attribute joins run in Rust. When an application callback can inspect the
getter, the binding constructs its Python function and closure through the
CPython object API. Calling that getter from application code remains a user
callback; the standard getter used by native join remains implemented in Rust.

Imports publish their bindings separately from public assignments. At module
completion the binding copies the context's exported mapping, including user
mutations and `get_exported` overrides. The VM retains native macro owners
alongside their Python facades.

What still runs through a Python helper instead of Rust is recorded in
`compatibility/spec_contracts.json` and summarized in
[STATUS.md](STATUS.md#what-still-runs-python).

## The native bridge

Templates always execute in the MiniJinja VM through PyO3, which holds owned
CPython references for lookup, operators, iteration, callbacks and formatting.
Neither `docxtpl.DocxTemplate.render` nor `jinja2.Template.render` is ever
called — the tests replace both with failures — and no error is retried through
a Python template executor.

The binding has two document paths:

| What the template and the context use | Who owns the document |
| --- | --- |
| The default environment and plain data | The native `docxtplrs` package, its own OPC writer and libxml2 policies |
| Objects, callbacks, a supplied `Environment`, pictures, or an exposed `Document` | The real python-docx/lxml graph: native rendering with explicit body, header and property commits |
| File subdocument composition | The native package and composer |

On the exposed graph the original types and retained references survive: the
body source is read before compilation and replaced before the header
callbacks run, old paragraph and run references stay on the old tree (including
a cached `Document._body` wrapper), header and footer targets become new
`XmlPart` objects with copied relationships, and property setters run in order,
leaving earlier commits visible when a later one fails. After a successful
render, `get_docx` reloads the original source, as the reference does.

`RichText`, `RichTextParagraph` and `Listing` stay live XML builders: a
callback receives the object and the output reads its current XML. The
programmatic `Subdoc` builder and picture construction use python-docx and
docxcompose objects with native template execution — an explicit Python API
integration, not a Rust port of those helpers.

The fork separates attribute from item lookup, makes truth testing fallible and
keeps Python's own operators, arbitrary integers and scalar identity. Supplied
`Environment` and `Undefined` subclasses keep their own lookup and undefined
behaviour instead of being flattened into the default configuration. Features
without an adapter (compiler, cache, syntax and policy hooks) fail explicitly
rather than silently degrading.

Compilation validates the source with the language the user supplied, which is
also when constant output and finalizer callbacks run; execution stays native.
A closed frontend skips the Python parser for a grammar it owns completely —
paths, `if`, `for`, `set`, macros, comparisons, the standard filters and tests,
arithmetic and calls — with a data-only context and a plain environment; the
first source outside that grammar disables the optimization for the parts that
follow. [API.md](API.md#templates-parsed-without-jinja2s-parser) states the
qualified grammar.

Rust document cores and parser caches stay local to detached calls. No raw lxml
node crosses into the system libxml2: the persistent native hyperlink graph
owns bytes and strings, and the model adapter commits its own nodes.

### Hyperlinks before rendering

`build_url_id` keeps an owned native source graph until rendering starts. An
external hyperlink reuses an existing target and type, and takes the first free
canonical `rIdN`, gaps included. The source ZIP is serialized once at the
render or save boundary instead of once per hyperlink. On an exposed model,
relationship creation delegates to the original part graph, including calls
during rendering, and a target that is not a string is kept exactly as
python-docx keeps it.

## The laboratory

`paritylab` renders every case twice, with the pinned docxtpl and with the
engine, in separate processes, and compares text, complete XML and binary
parts, validity, size and rendered pixels. It also compares what happens
during a render: callback arguments, object identity, exception order. The
method is in [CONTRACT.md](CONTRACT.md).

The laboratory is the reason the forks exist. A difference found by a
comparison becomes a hook or a fix in the engine, never a special case in the
comparator.

## Boundaries still to establish

| Area | Current implementation | Boundary to establish |
| --- | --- | --- |
| DOCX templating | `docxtpl-core` uses a typed JSON payload and the forked engine | A native typed API with explicit ownership and no language-binding dependency |
| OPC package and relationships | `docxtpl-core::opc` consumes `docxtplrs::package::Package` | Separate generic package operations from python-docx serialization policies and test the intended OPC contract |
| Word document model | `docxtplrs` and the binding both expose partial document models | One authoritative model, explicit reference and mutation semantics, and OOXML-specific tests |
| Document composition | Composer lives in the `docxtplrs` fork | A reusable API only after styles, numbering, relationships and binary parts are validated |
| Jinja runtime | Audited MiniJinja fork and `rsyi/jinja` dependency | Track local changes against upstream; define compatibility and runtime extension boundaries before separating a new project |
| Python integration | `bindings/python` and `python/docxtpl_native` | Keep Python object protocols and API adaptation at the edge; port document and template rules to Rust |
| Archive buffer reuse | The package fork owns compressed-entry and archive storage | Keep ownership and range checks internal until a second real consumer establishes a reusable API |

A reusable boundary needs a concrete consumer, a documented scope, independent
unit and integration tests, error and ownership semantics, and compatibility
evidence for the relevant file format. Internal crates can evolve together in
a workspace; a separate crate is not automatically a stable public API. Do not
introduce an interface solely for hypothetical reuse, and do not change
document behavior to force a crate boundary.

Hot paths should avoid unnecessary allocations and copies. Prefer compile-time
dispatch when it fits; where dynamic dispatch is justified, measure it. Compare
release builds on equal outputs, retain raw samples, and report regressions as
well as gains.

## Consuming the engine from another workspace

Depend on the crates by git revision and repeat the two patches. Cargo does
not inherit patches declared by a dependency's workspace:

```toml
[dependencies]
docxtpl = { git = "https://github.com/talismanai/docxtpl-native", rev = "<commit>", features = ["pycompat", "wasm"] }

[patch.crates-io]
minijinja = { git = "https://github.com/talismanai/docxtpl-native", rev = "<commit>" }
zlib-rs = { git = "https://github.com/talismanai/docxtpl-native", rev = "<commit>" }
```

`docxtpl` is a facade over the engine crates, one feature each, so that a
consumer writes the revision once in `[dependencies]` instead of once per
crate. The two patches are still its own: Cargo reads a patch table only from
the workspace root being built, and the names come from third-party crates
(`jinja` asks crates.io for MiniJinja, `flate2` for zlib-rs), so a facade
cannot hide them. A consumer that handles template values takes MiniJinja from
the facade (`docxtpl::minijinja`) rather than declaring it again: two
MiniJinja crates in one graph are two unrelated sets of types.

Keep every entry on the same revision. Do not enable a C backend of `flate2`
(`zlib`, `zlib-ng`): it takes precedence over zlib-rs and changes the bytes of
the archive.

A consumer of `docxtpl-core` also needs the libxml2 build dependencies. The
project in `crates/docxtpl-core/tests/external-consumer` is a separate
workspace that renders a real DOCX this way, without Python, and CI runs it.

Git-pinned dependencies, the required patches and the exposed fork types are
why the crates are not published on crates.io yet.
