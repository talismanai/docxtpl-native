# Compatibility hooks fork

Upstream docxtplrs 0.3.2, commit
`93daab8b6441bfb2e356007ab13b8eb884148d1f`, declared LGPL-2.1-or-later.
Every imported source, manifest and README file is recorded with its upstream
Git blob hash in UPSTREAM.json, which scripts/check_vendor.py verifies.
The upstream revision contains no LICENSE file; the declared LGPL-2.1 text is
provided here from this project's standard license file. Upstream source and
project metadata remain intact, with the local delta recorded in UPSTREAM.json.

The host fragment boundary can carry Unicode code points that do not fit in a
Rust string to an explicitly configured materializer. The ordinary UTF-8 path
remains unchanged; a missing Unicode materializer is an error.

The opt-in per_part_context field snapshots the caller context for individual
parts/properties. By default the upstream cached context is reused.
An optional function-pointer property_renderer hook lets the caller supply its
own XML/property contract without raw pointers or reentrant core borrows.
An optional host template renderer receives an independent environment and a
named or owned compilation, without receiving the package core. The Python
binding uses it to drive each asynchronous part in the calling thread's task;
the default Rust rendering path remains unchanged.
The opt-in python_image_contract retains detached-inline whitespace and uses
the uncommitted part's shape ID for each image, as python-docx does. The main
body's later docPr renumbering preserves those original Picture names.
The Python source contract skips upstream helper rewrites for operators,
globals and extension tags, since the binding supplies the original protocols
and validates the source language before native compilation.
Subdocument compatibility maps style IDs by name and leaves the numbering
catalog unchanged when no merged story references numbering or styles. The
general numbering/style/section composition contract remains unqualified.
An optional header/footer normalizer runs at source-load and replacement
boundaries. The binding supplies strict libxml2 blank-node handling and UTF-8
serialization to match the original XmlPart replacement operation.
The Python source contract compiles each part before snapshotting the caller's
mapping, so compile-time effects or errors precede context conversion.

`join_split_jinja_tags` exposes the first two `patch_xml` substitutions on
their own, for a caller that reads or rewrites a part's jinja and must get the
part back without the rest of `patch_xml`'s rewrites. Both substitutions, and
their gates, are the ones `patch_xml` already runs; no behavior changes.

docxtpl-native enables those hooks and implements the six property assignments
with libxml2. This includes snapshots for empty properties, namespace-URI lookup,
the 255-character setter limit and XML text validation. It does not certify the
entire document object model, composition or public Python API.

The upstream Python feature and its original bridge source are retained for
provenance, but the feature is not a default: `default = []`, so a
dependency on this crate is Rust only unless `python` is requested. The library
is built as an rlib only: a cdylib target makes Cargo write the rlib of this
path crate without a hash in its file name, so builds with different feature
sets would replace each other's output. This application builds with that feature disabled and uses its own
owned-reference bridge. CI runs this crate's inline library tests with Python
disabled; the full upstream external example corpus remains a separate gate.

One inherited namespace test expected lowercase `true`. Pinned MiniJinja 2.24
already formats that value as `True`, matching Jinja2 3.1.6; the stale test
expectation is corrected. The independent namespace_state protocol probe checks
the reference and native document path as well.

The rich-text generator emits font properties immediately after style, matching
the fixed Python reference when font is combined with other properties. The
binding tests combined properties and implements object-protocol dispatch in Rust.

For shared input archives, lazy compressed media stores checked ranges into
the original archive allocation rather than a separate copy. Non-shared
archives retain the owned fallback. Tests check source identity and lifetime.

Rich-text runs stream their properties and escaped text into one output buffer.
The binding's differential tests check exact XML against the pinned reference.

In Python compatibility mode, file-based subdocuments retain their original
bookmark IDs when inserted. The reference renumbers the master at attachment,
not the detached subdocument body; the full master attachment phase remains open.

The Python style-mapping path schedules referenced styles in story order,
placing newly imported linked styles after their first referenced style.

Before retaining a compressed media range for lazy access or raw copying,
the loader streams it through the ZIP reader to verify deflate and CRC without
allocating the decompressed payload. The additional CPU cost needs benchmarking.

For file subdocuments present in the Python render context, the binding applies
the master bookmark and drawing identifier pass before rendering. Inserted
subdocument picture identifiers remain untouched. Effects observed before render
and subdocuments absent from the context need a constructor-time adapter.
The same XML normalizer removes blank structural nodes when loading a file
subdocument in Python compatibility mode, matching python-docx part loading.

ZIP replacement traversal applies updates in archive order, invalidates media
and relationship caches, and preserves source compression and directory
entries for the post-processing path. Tests cover partial mutations on failure.
The compatibility footnote locator selects parts and repetitions from the
package and rendered body; it retains invalid UTF-8 for the binding to report.
Template newlines are normalized before rendering, and paragraph delimiter
helpers are exposed to the binding. These hooks remain subject to the broader
compatibility and performance gaps in the status document.
Escaped delimiter restoration applies the four substitutions in reference
order, including matches formed by earlier overlapping substitutions. A
single-pass path is used when no overlapping markers can change that result.

Generated OPC packages opt into `to_bytes_opc`: recompress all generated parts
with the pinned Rust zlib-rs level-6 strategy, omit directory records, and adapt
central metadata/ZIP64 to the Python writer. Untouched binary parts are content,
not trusted canonical compressed streams. Replacement saves retain their
existing preservation mode. The metadata adapter leaves compressed payloads
and local headers to ZipWriter. A laboratory example checks complete archives,
including reserved local ZIP64 and 65535/65536-entry boundaries.

OPC recompression streams untouched packed media through the decoder into the
ZIP writer, retaining compressed-size capacity hints. It does not materialize
a decoded Arc solely for saving; already accessed values retain their shared
storage. This removes an expanded-media allocation from generated saves.

The OPC directory end-record encoder is exposed for the binding's interrupted
save finalizer. Both paths use the same ZIP64 thresholds and footer bytes;
compressed payloads and local headers are not rewritten during finalization.

Python source compatibility renders only the document body, carrying inherited
namespace declarations into the standalone template, and replaces that body in
the original document XML afterward. A namespace-aware reader identifies the
element and preserves the surrounding bytes. The separate Rust-native path
keeps its existing whole-part contract. This does not establish parity for
callback-visible mutations of the document graph or all XML recovery cases.

Deferred materialization accepts ordered text and explicitly tagged indices;
it never parses text in that API, and repeated indices reuse the same
materialization. The default renderer supplies those fragments with the
upstream marker scanner, so it does not establish output provenance or
prevent marker collisions.
An additional opt-in pipeline carries explicit deferred fragments through
paragraph, delimiter and listing transforms before materialization. It chooses
a temporary transport prefix absent from a transformed text probe and retains
copied occurrences. The default render paths do not provide typed fragments.
An opt-in part-rendering mode retains host fragments through captures before
the root result is joined; the upstream marker scanner handles that result.
An optional host-owned part scope wraps compiled evaluation and permits the
caller to stage package state without a mutable global pointer; the default
path is unchanged. A single deferred value may be materialized through the
public core API. The typed caller materializes images eagerly; subdocuments
and marker-free materialization are incomplete.
The typed caller can opt into the existing no-reference numbering guard
independently of the Python source-loading contract. Referenced styles and
numberings use the upstream import path; this does not qualify their
mapping or the full docxcompose numbering algorithm.
The guard parses element names conservatively, recognizing alternate prefixes
and retaining imports on unclosed elements or parser errors. This is not a
general XML validation pass.
For the typed caller, the no-numbering path also applies before styles are
merged when every referenced style and its dependencies already exist with
identical definitions, names remain unambiguous ignoring ASCII case, and no direct numId occurs.
Other cases use the upstream numbering importer.
The typed restart path finds numbering and outline markers inside style
descendants and follows the reference's insertion order for new numbering
instances and paragraph properties. General numbering selection is not
implemented.
For one direct numId in the body without style references or auxiliary
stories, the typed path copies only the referenced num and abstractNum, uses
the reference's insertion order and remaps the body. The copied definition's
nsid is derived from the definition (`numbering::stable_nsid`) instead of
docxcompose's random draw, so renders are reproducible (intentional
difference, docs/STATUS.md). An optional host protocol adapts abstract ID
integer conversion; without it the pure Rust API uses native integer parsing.
Multiple IDs, styles, picture bullets and auxiliary stories use the upstream
importer and are unqualified.
The typed caller converts unjoined VM output to owned text/deferred fragments
and materializes after XML transformations without scanning user text for
marker tokens. The typed caller can use a host XML recovery parser when
the rendered main document contains forbidden XML 1.0 characters. Other rendering paths,
including core properties, use the upstream marker scanner; general recovery
and all provenance-losing operations are unqualified.
The optional deferred-image metadata field lets a host supply dimensions and
resolution parsed from a live stream; callers without it retain the existing
blob parser. The host owns stream protocols and their error translation.

# Host capture joining

The optional host capture-joining mode uses MiniJinja's nested-fragment
renderer while retaining the ordinary root text stream and materialization
path. It is disabled by default.

An additive host fragment renderer may return VM values directly to the existing
fragment converter, including when a host owns execution. It rejects a missing
converter or a render error without falling back to the string renderer. This
hook alone does not connect the Python bridge or establish marker safety there.

The host may cache an early subdocument materialization per index and part
for the current render; render initialization clears that cache. Image
materialization is not cached by this mechanism. Cross-part composition and
callbacks mutating an attached subdocument remain unqualified.

The host can initialize a render, attach registered file subdocuments in
their supplied order, and then continue via `render_initialized`. Their
detached body XML is shared across rendered parts without reattaching package
parts; render initialization clears this cache. This does not qualify live
subdocument mutation or every construction and error-ordering protocol.

For Python source compatibility, the existing unchanged-style guard also
skips copying numbering for styles already identical in the master. Direct
numbering and changed, ambiguous or missing styles take the upstream path.
The existing-style path uses the reference-compatible list restart ordering.

For the typed Python-compatible contract, file-subdocument descriptors cause
the master document's existing picture and drawing IDs to be renumbered before
template rendering, in relationship order. Pictures introduced during render
are not included in that constructor-time pass. This does not establish the
construction order of several separately created subdocuments or the general
live-object protocol.

The typed constructor-time drawing pass and its post-render docPr pass use
quick-xml namespace resolution, including default or rebound prefixes, and
scope main-document changes to descendants of the Word body. This does not
qualify other XML transformations, which match literal prefixes.

The host may install a stateful body XML recovery adapter. It takes precedence
over the existing function-pointer recovery and propagates errors directly.
Without the adapter, the function-pointer recovery applies.

`make_env` enables MiniJinja's Jinja-compatible newline normalization, so
loaded templates and property strings that bypass the part preprocessor see
LF newlines as in Jinja.

When the rendered body is spliced back into the document, namespace
declarations follow lxml's `moveNodeToDocument` (docxtpl `map_tree`): across
the adopted subtree, a declaration whose URI is already in scope from the
parent under an unshadowed prefix is removed and its references renamed to
that prefix. A declaration that could only map onto a default namespace is
kept; lxml's additional attribute re-declaration for that case is not
reproduced.

Clock reads are optional so the crate runs on `wasm32-unknown-unknown`, which
has no clock and panics in `Instant::now`/`SystemTime::now`: patch timing reads
the clock only when `PATCH_TIMING` is set, and random seeds, lorem ipsum and
comment timestamps use their existing fallbacks when no Unix time is available.

`-%}` whitespace control merges with the next text as docxtpl's
`-%}(?:(?!<w:t[ >]|{%|{{).)*?<w:t[^>]*?>` does, with one deliberate
difference (docs/STATUS.md): the merge ends at the next `<w:t>` element and
removes nothing when a `{%`, a `{{` or a table, row or cell boundary comes
first. docxtpl's pattern also stops at `<w:tabs>`, `<w:tbl>`, `<w:tc>` and
`<w:tr>` and deletes the markup before them, which breaks tab stops and
tables. `{%-` applies the same table, row and cell guard in the other
direction.

`recover` reproduces libxml2 2.14 recover mode (lxml `recover=True`, used by
docxtpl `fix_tables`) for the errors rendered templates produce: a mismatched
end tag closes the current element, predefined entities in text are dropped
after the first error, invalid references, stray `<` and forbidden characters
are dropped, content after the root ends parsing. In the typed Python-numbering
contract the rendered body is recovered this way before fixing tables when it
is not well-formed; a differential fuzz against lxml is in
`scripts/recover_fuzz.py`. `recover_xml`, a heuristic recovery, also escapes
stray unprefixed end tags.

`Cycler.current` returns the item `next()` returns, as `jinja2.utils.Cycler`
does. `py_like_method` is public so hosts can extend the Python method set.

With the Python numbering contract, creating a file subdocument also renumbers
the master body's bookmarks as docxcompose's `Composer.renumber_bookmarks`
does under docxtpl 0.17 (`SubdocComposer.attach_parts`): bookmark starts, then
ends, take ids 0, 1, ... in document order before rendering.

`recover` ends a start tag with a malformed attribute as libxml2's recover
mode does: an attribute without a value directly before `>` is dropped and the
element stays open; otherwise the element is empty and the rest, from the next
non-blank character (or from an unquoted value), is text. A rendered object
`repr` such as `<reversed object at 0x...>` produced this. With the Python
source contract, a body that the table fix cannot parse and that is not
well-formed is recovered first, as docxtpl's `fix_tables` parses it with lxml's
recover mode. `recover_document` recovers a whole document's body and drops
namespace declarations it repeats from the root. Tests in `recover.rs` pin the
lxml outputs.

The element tag pass (`{%p %}`, `{%tr %}`, `{%tc %}`, `{%r %}` and `{#y #}`)
follows docxtpl's `<w:y[ >](?:(?!<w:y[ >]).)*({%|{{)y ([^}%]*(?:%}|}})).*?</w:y>`:
an element keeps the LAST well-formed tag before the next opening of its kind
(the tempered dot is greedy), either closing token ends the tag, an element
with no closing tag after the marker is kept, and only the nearest opening
before a marker can take it, so a `{%tr %}` in a nested table replaces the
inner row instead of the outer one. Space preservation follows
`<w:t>((?:(?!<w:t>).)*)({{.*?}}|{%.*?%})`: the opener needs its closer after
it, and the match runs to that closer. The clean pass follows Python's
`re.sub` of `(?<=\{[\{%])(.*?)(?=[\}%]})` (no match across a newline; an
empty match is retried for a non-empty one). The fused strip/space/clean scan
is used only when `fused_scan_exact` shows it equals those passes. Tests with
expected values from Python's results are in `patch.rs`; a test that pinned
an unclosed element being replaced was corrected against docxtpl.

# quick-xml 0.42

The manifest requires quick-xml 0.42 instead of upstream's 0.36, for
RUSTSEC-2026-0194 and RUSTSEC-2026-0195. `xml_events` keeps the event
contract the parsers were written for: quick-xml reports a reference in
character data as its own `GeneralRef` event and rejects a `&` that starts no
reference, so every reader accepts a lone `&` as text again, and `TextRuns`
joins the pieces of each run of character data back into one text event with
the run's source text. `xmldom`, `recover` and `subdoc` unescape that source,
and attribute values, with `quick_xml::escape::unescape`, as quick-xml 0.36's
`unescape` and `unescape_value` did; attribute values are not normalized.
Name, namespace and attribute comparisons are on `str` with the same bytes.
Tests named `reference_tests` pin the results of the 0.36 parsers. The
namespace readers keep quick-xml's default limit of 128 namespace bindings in
scope; a document beyond it is a parse error.
