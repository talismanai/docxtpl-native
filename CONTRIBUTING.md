# Contributing

The project rules are in [AGENTS.md](AGENTS.md). They apply to people and to
coding agents. This page shows how to build, test and release.

## Layout

| Path | Contents |
| --- | --- |
| `crates/` | The engine (`docxtpl-wasm`, `docxtpl-pycompat`, `docxtpl-markers`, `docxtpl-pdf`), the `docxtpl` facade over them, and the host core (`docxtpl-core`) |
| `js/` | `@docxtpl/wasm`, the host side of the WebAssembly ABI |
| `vendor/` | Audited forks: `docxtplrs`, `minijinja`, `zlib-rs` |
| `bindings/python` | The PyO3 extension crate |
| `python/` | `docxtpl_native` (the Python package) and `paritylab` (the laboratory) |
| `tests/`, `scripts/` | Python tests of the binding and tools of the laboratory |
| `compatibility/` | The contract catalog, the accepted-failure map, the performance baseline and the pinned renderer |
| `docs/` | [Documentation index](docs/README.md) |

## The engine (Rust only)

You need Rust 1.96.0 (`rust-toolchain.toml` selects it) and, for
`docxtpl-core`, the libxml2 and libclang headers with pkg-config
(`libxml2-dev libclang-dev pkg-config` on Debian or Ubuntu). No Python.

The engine's own minimum is still 1.93; `docxtpl-pdf` is what needs the newer
compiler, because the PDF converter it wraps uses newer language features.

```bash
cargo test --release --locked                              # engine and host core
cargo test -p docxtpl-markers -p docxtpl-pdf --release --locked
cargo test -p docxtplrs --lib --release --locked           # DOCX fork
cargo test -p minijinja --lib --all-features --release --locked
cargo clippy --release --locked --all-targets
bash scripts/check_engine_boundary.sh                      # no Python in the engine
bash scripts/check_versions.sh                             # one version everywhere
```

`cargo test --release --locked` builds the default members; the marker and PDF
layers are not among them, so that an ordinary engine build does not fetch the
PDF converter. The line above runs them.

The browser module, and the whole set a browser pipeline uses:

```bash
rustup target add wasm32-unknown-unknown
cargo build -p docxtpl-wasm --target wasm32-unknown-unknown --profile wasm --locked
cargo build -p docxtpl --features browser --target wasm32-unknown-unknown --profile wasm --locked
```

The host side of the WebAssembly ABI, which has no dependencies to install,
and the check that keeps the modules free of unexpected imports:

```bash
node --test js/test/ scripts/check_wasm_imports.test.mjs
node scripts/check_wasm_imports.mjs target/wasm32-unknown-unknown/wasm/docxtpl_wasm.wasm
```

The Jinja2 builtins are checked against Jinja2 on generated cases, on the host
and in the `.wasm` (it needs Jinja2 installed):

```bash
cargo build --release --locked -p docxtpl-wasm --example conformance
python scripts/wasm_jinja_conformance.py target/release/examples/conformance
python scripts/wasm_jinja_conformance.py node scripts/wasm_conformance_runner.mjs \
  target/wasm32-unknown-unknown/wasm/docxtpl_wasm.wasm
```

Two cases of the UTF-16 recovery test pin different outputs on system libxml2
2.9.13 and on the newer libxml2 bundled by lxml 6. Neither case is skipped;
passing these version-specific checks does not demonstrate lxml parity on
2.9.13. Older versions are checked against the measured 2.9.13 expectations;
2.14 and later use the reference expectations. Other library versions still
need independent validation.

## The Python binding and the laboratory

You need the above plus Python 3.11 or 3.12. The examples use
[uv](https://docs.astral.sh/uv/).

```bash
uv venv --python 3.12
uv pip install 'maturin==1.16.0' 'python-docx==1.2.0' \
  'Jinja2==3.1.6' 'lxml==6.1.1' 'docxcompose==1.4.0' 'setuptools==80.9.0' \
  'Babel==2.18.0' 'Pillow==13.0.0' 'PyMuPDF==2.1'
source .venv/bin/activate
maturin develop --release --locked
cargo test -p docxtpl-native --lib --release --locked      # Rust tests of the binding
python scripts/run_product_tests.py                        # product tests and audits, docxtpl blocked
python scripts/run_product_tests.py --tier audit           # native execution audits only
```

The package never imports docxtpl. `scripts/run_product_tests.py` runs every
test module that does not reach docxtpl with all imports of docxtpl blocked
and recorded (`--list` prints the split). Exit status 3 means the product used
docxtpl. `tests/test_reference_free.py` runs a complete user workload in a new
interpreter with the same blocker.

The distributed package (`python/docxtpl_native`) has no Ruff or ty
diagnostics, and CI keeps it that way. Names the native extension reads from a
module's globals are listed in that module's `_NATIVE_GLOBALS`, so an import
that looks unused is not removed:

```bash
uvx ruff@0.17.5 check python/docxtpl_native
uvx ty@0.0.104 check --python .venv python/docxtpl_native
```

Native execution audits fail when a standard Python algorithm runs instead of
the engine. Write them with `tests/native_audit.py` (`forbid(...)`), keep them
free of docxtpl, and put the comparison of their output with the reference in a
differential module such as `tests/test_audited_documents_reference.py`.

Every test the full suite expects to fail is listed, with its cause and
contracts, in `compatibility/known_failures.json`: `failures` is pending work
and `divergences` are accepted differences from the reference (each cause
records its decision; see [docs/STATUS.md](docs/STATUS.md#accepted-debts)).
Check a run against it; a new failure, a listed test that passes or a changed
failure kind makes the check fail. A test that starts to pass leaves the list
in the same change; an accepted divergence that changes needs a new decision:

```bash
PYTHONPATH=tests:python python -m unittest discover -s tests -v > suite.log 2>&1
python scripts/check_known_failures.py suite.log
```

To check a wheel in a clean environment, without docxtpl:

```bash
maturin build --release --locked --out dist
python scripts/check_wheel.py dist                         # payload; no docxtpl requirement
python scripts/check_reference_free_wheel.py dist/*.whl    # fresh venv with uv, then the workload
```

The extension links the host's libxml2, so a wheel built on an ordinary
distribution is only valid there (`--compatibility linux`). A portable wheel is
built inside the manylinux image, where auditwheel copies libxml2 and liblzma
into it and retags it `manylinux_2_28`:

```bash
docker run --rm -v "$PWD:/work" -w /work -e CARGO_TARGET_DIR=/tmp/portable-target \
  quay.io/pypa/manylinux_2_28_$(uname -m) bash scripts/build_portable_wheel.sh 3.12
docker run --rm -v "$PWD/dist:/dist:ro" python:3.12-slim bash -ceu '
  ! ldconfig -p | grep -q libxml2                          # no libxml2 anywhere
  pip install -q /dist/*manylinux*.whl "python-docx==1.2.0" "Jinja2==3.1.6" "docxcompose==1.4.0"
  python -c "import docxtpl_native; print(docxtpl_native.capabilities())"'
```

The differential tests and the laboratory also need the reference. Install it
only from the pinned checkout, never from the package index: the same version
number on PyPI does not prove the same source.

```bash
python scripts/fetch_upstream.py
uv pip install --no-deps .upstream/docxtpl
PYTHONPATH=tests python -m unittest discover -s tests -v   # whole suite; also inherited by subprocess tests
python scripts/inventory.py
python -m paritylab.cli --suite all --visual required --repeat 7 --out artifacts/parity
python scripts/compatibility_matrix.py --check
python scripts/spec_coverage.py
```

While iterating, `--jobs N` evaluates N cases at a time (same verdict, no
timings) and `--rerun-from artifacts/parity/report.json` keeps only the cases
that did not pass. Both leave the report marked as what it is: a rerun is a
partial run, and a partial run qualifies no revision.

`patch_xml`, the tag preprocessing every part goes through, also has a
differential fuzzer: random WordprocessingML, Jinja delimiters and docxtpl's
structural tags through both engines, which must return the same string or
raise the same exception type.

```bash
PYTHONPATH=python python scripts/patch_xml_fuzz.py 10000 1   # cases, seed
```

When the pinned reference revision changes, regenerate the manifest of its
example scripts and fixtures, which the laboratory checks before running them:

```bash
python scripts/generate_upstream_manifest.py --repo .upstream/docxtpl \
  --revision <commit> > python/paritylab/upstream_manifest.py
```

Visual comparison needs LibreOffice Writer and PyMuPDF. See
[docs/CONTRACT.md](docs/CONTRACT.md#the-visual-comparison) for the pinned
renderer and how its own stability is measured.

To check the laboratory itself before the extension is built:

```bash
PYTHONPATH=python python -m paritylab.cli --candidate reference --suite synthetic --visual off --repeat 2 --out artifacts/self-check
```

That run is labelled `self_check`. It is never native parity and reports no
speedup.

Private documents go in the ignored `fixtures/private/`. Copy
`fixtures/real.example.json`, then run
`python -m paritylab.cli --real-manifest fixtures/real.json --visual required`.
Never commit them or upload them as CI artifacts.

## Changing a fork

`vendor/docxtplrs`, `vendor/minijinja` and `vendor/zlib-rs` are forks with
recorded provenance. Each has:

- `UPSTREAM.json`: the upstream repository and commit, the Git blob hash of
  every imported file, and the hash of every file changed (`local_changes`) or
  added (`local_files`) here.
- `FORK.md`: what differs from upstream, and why.

When you change a file in a fork:

1. Prefer a hook the engine opts into over a change of default behaviour.
2. Describe the change in `FORK.md`.
3. Update the file's hash in `UPSTREAM.json`
   (`git hash-object vendor/<fork>/<path>`).
4. Run `python3 scripts/check_vendor.py`. It rejects any file that matches
   neither its upstream hash nor a recorded change.

Reuse an existing Rust library before writing a component, and record every
new dependency, fork or adapted behaviour in [THIRD_PARTY.md](THIRD_PARTY.md).

## Recording results

Raw results are not versioned: every run writes to `artifacts/`, which is
ignored. A change to coverage or to an execution boundary states the new
state in [docs/STATUS.md](docs/STATUS.md) next to the command that
reproduces them, so a reader can run it instead of trusting the number. Keep
machine names, user names, private data and absolute local paths out of
anything committed.

## Pull requests

The rules a change follows are in [AGENTS.md](AGENTS.md): the engine and
open-source boundaries, forks, evidence, tests and documentation.

- CI must pass the `engine`, `provenance` and `python-binding` jobs. The
  binding job accepts exactly the failures listed in
  `compatibility/known_failures.json` and no gap in the differential corpus.

## Releases

Releases are automatic. To publish one:

1. Set the new version in `[workspace.package]` of `Cargo.toml`, in
   `pyproject.toml` and in `python/docxtpl_native/__init__.py`
   (`scripts/check_versions.sh` fails when they differ). Then let the lock
   files follow: `cargo update --workspace`, and the same command with
   `--manifest-path crates/docxtpl-core/tests/external-consumer/Cargo.toml`.
2. Rename the `Unreleased` section of `CHANGELOG.md` to the version and open a
   new empty `Unreleased`.
3. Merge to `main`.

The `release` workflow sees that the tag `v<version>` does not exist and that
`CHANGELOG.md` has a `## [<version>]` section, runs the
engine gates, builds the browser module (`docxtpl_wasm-<version>.wasm`), two
Linux wheels per Python 3.11 and 3.12 (one `linux`, one portable
`manylinux_2_28`) and the source distribution, then
creates the tag and a GitHub release with checksums. The notes start with the
version's CHANGELOG section, followed by the list of merged changes. A push
that does not change the version, or whose version has no CHANGELOG section,
releases nothing.
