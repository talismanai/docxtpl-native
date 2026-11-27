# Dependencies and provenance

| Project | Pinned revision / version | Declared license |
| --- | --- | --- |
| MiniJinja compatibility fork | 2.24.0; upstream 0ca749f7ba507514fa6b052c74130ae6ae472e03, audited local delta | Apache-2.0 |
| docxtplrs compatibility fork | 93daab8b6441bfb2e356007ab13b8eb884148d1f, audited local delta | LGPL-2.1-or-later |
| zlib-rs compatibility fork | 0.6.8; upstream 0254e07204884bfc902d6ebb68c8b703d0ea49cf; level-6 strategy delta in vendor/zlib-rs/FORK.md | Zlib |
| rsyi/jinja | 52d077512af9b943b005ef374412ec96ee3a7a3d | Apache-2.0 |
| docxide-pdf, with preview optimizations | 0.22.0; the fork talismanai/docxide-pdf at tag `v0.22.0-talisman.5`: upstream sverrejb/docxide-pdf fe2cd29fb (host/WASM APIs, fast compression, layout optimizations) with the fork's bookmark range links (floating pictures included) and picture resolution, draft picture and picture cache options; used only by crates/docxtpl-pdf | Apache-2.0 |
| docxide-pdf test font | `nb-hyphen-ascii.ttf`, a synthetic font (generated outlines, family "NB Hyphen Fixture ascii") from `src/fonts/testdata`, in crates/docxtpl-pdf/tests/fixtures | Apache-2.0 |
| corosensei | 0.3.4; registry checksum in Cargo.lock | MIT OR Apache-2.0; MIT notice in vendor/notices/corosensei-LICENSE-MIT |
| scoped-tls | 1.0.1; registry checksum in Cargo.lock | MIT OR Apache-2.0; MIT notice in vendor/notices/scoped-tls-LICENSE-MIT |
| scoped-tls-hkt | 0.1.5; registry checksum in Cargo.lock | MIT OR Apache-2.0; MIT notice in vendor/notices/scoped-tls-hkt-LICENSE-MIT |
| CPython constant-pool and string-interning behavior | v3.12.14, Python/compile.c and Objects/codeobject.c; binding adaptation checked on 3.11/3.12 | PSF-2.0 and retained historical notices; vendor/notices/CPython-LICENSE |
| Jinja2 runtime protocols, standard predicates/filters and AST constant evaluation | 3.1.6, jinja2/runtime.py, jinja2/compiler.py, jinja2/tests.py, jinja2/filters.py and jinja2/nodes.py; Rust adapters in bindings/python/src/native_tests.rs, native_filters.rs and constant_eval.rs | BSD-3-Clause; vendor/notices/Jinja2-LICENSE |
| MarkupSafe conversion and escaping behavior | 3.0.4, markupsafe/__init__.py | BSD-3-Clause; vendor/notices/MarkupSafe-LICENSE |
| rust-libxml | 30e931c217998ee932a0b29c25849199a3dd1f83 | MIT |
| libloading | 0.8.9; checksum in Cargo.lock; direct access to the tree-owning C library | ISC; vendor/notices/libloading-LICENSE |
| lxml C object ABI and serialization rules | lxml 6 ABI with runtime layout/symbol checks; 6.1.1 serializer.pxi/proxy.pxi behavior adapted in crates/docxtpl-core/src/foreign_xml.rs | BSD-3-Clause; vendor/notices/lxml-LICENSE |
| PyO3 | 0.29.0; checksum in Cargo.lock; used only by the Python binding | MIT OR Apache-2.0 |
| image, png, zune-jpeg, tiff | 0.25, 0.18, 0.5 and 0.11; checksums in Cargo.lock; picture decoding in crates/docxtpl-pycompat | MIT OR Apache-2.0 (image, png); MIT OR Apache-2.0 OR Zlib (zune-jpeg); MIT (tiff) |
| quick-xml, fancy-regex, zip, flate2 | Versions and checksums in Cargo.lock; quick-xml 0.42 retains the bounded namespace resolver and adapts split entity events to preserve existing XML text semantics; regular expressions and the ZIP container in the engine | MIT (quick-xml, fancy-regex, zip); MIT OR Apache-2.0 (flate2) |
| Pillow picture semantics | Pillow 12: how a picture is identified, measured (`info["dpi"]`) and rotated; behavior reproduced in crates/docxtpl-pycompat/src/images.rs and checked by scripts/pillow_images_parity.py | MIT-CMU |
| docxcompose composition behavior | 1.4.0: style, numbering and bookmark handling when a subdocument is merged; behavior reproduced in vendor/docxtplrs and the binding | MIT |
| python-docx-template (docxtpl) behavior, public API and reference corpus | 0.20.2 as the behavioral reference; the Python package reimplements its class signatures and command line (python/docxtpl_native, docs/API.md) without importing it; corpus at 1f143fbe86c19ecb28c3205d5f4b1547c7e2d7ad, fetched by scripts/fetch_upstream.py and installed only for the laboratory and differential tests, not redistributed here | LGPL-2.1-only |
| Python runtime dependencies of the package | python-docx 1.2.0, Jinja2 3.1.6, lxml 6; extra `subdoc`: docxcompose 1.4.0 and setuptools < 81 (docxcompose imports `pkg_resources`) | MIT (python-docx, docxcompose); BSD-3-Clause (Jinja2, lxml); MIT (setuptools) |
| python-docx OPC writer behavior / default-type table | 1.2.0 | MIT; vendor/notices/python-docx-LICENSE |
| Unicode word-category tables | UCD 14.0 and 15.0, from ICU release-70-1 and release-72-1, verified SHA-256 in `scripts/generate_unicode_word_tables.py` | Unicode data license; vendor/notices/UNICODE-LICENSE |

MiniJinja's source is retained under vendor/minijinja with LICENSE, authors,
upstream Git blob hashes and an explicit local-delta manifest. See its FORK.md.
The docxtplrs fork is retained under vendor/docxtplrs with the same provenance
checks and its declared license text; its FORK.md describes the two opt-in hooks.
Other dependency source trees are fetched separately at the pinned revisions,
retaining their licenses. Cargo.lock records the resolved dependency graph.
The Python object bridge uses the upstream minijinja-py protocol implementation
as an API/design reference; our implementation keeps render-local error state.
The Jinja2 `sync_do_join` attribute path and `TemplateReference` initialization
are adapted in Rust in the binding. Observable getter functions use CPython's
function/closure constructors and resolve builtins from their globals; the bridge
does not execute Jinja's getter factory to construct them.
Jinja's import publication and export rules are also adapted in the MiniJinja
fork and the binding: import targets update `Context.vars` without adding public
exports, while module completion honors `Context.get_exported` and retains the
native macro owners represented by its returned values.
The async binding uses corosensei's same-thread coroutine stack and scoped-tls
to expose its live yielder only during resume/unwind. It does not migrate VM
frames or CPython callbacks between threads; asyncio drives the task and awaits.
Foreign macro bodies reuse scoped-tls-hkt's borrowed callback support. The slot
is cleared around coroutine suspension and restored on resume/unwind; the binding
does not store raw VM frame pointers. Macro endpoints keep weak implementation
references, so a Python reference cannot outlive its Rust body unnoticed.

Every other crate in the dependency graph comes unmodified from crates.io at
the version and checksum recorded in Cargo.lock, under the license its
manifest declares (`cargo metadata` lists them). The engine crates link only
permissively licensed dependencies besides the LGPL docxtplrs fork.

The PDF host adapter also uses Apache-2.0/MIT-licensed `ttf-parser` 0.25 (already used by docxide-pdf) to preserve the face count returned by font registration.
