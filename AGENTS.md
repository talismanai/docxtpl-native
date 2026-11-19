# Project rules

For people and coding agents. [CONTRIBUTING.md](CONTRIBUTING.md) has the
commands; [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains the layers.

## Open-source boundary

- This repository holds a generic engine for docxtpl templates. Code, tests,
  fixtures, docs, commit messages and pull requests must make sense to any
  user of DOCX templating.
- Whatever is specific to one application (its document conventions, filters,
  styles, data, identifiers, parity scripts against its services) lives in
  that application's repository, which depends on this one by release tag.
- Never add names of organizations, products or customers, internal URLs,
  private identifiers, personal data or absolute local paths, in code, docs or
  generated artifacts: use paths relative to the repository root.
- Keep private fixtures, contexts and generated artifacts out of git and out
  of CI artifacts (`fixtures/private/` is ignored).

## Engine boundary

- The engine is Rust only: `crates/docxtpl`, `crates/docxtpl-wasm`,
  `crates/docxtpl-pycompat`, `crates/docxtpl-markers`, `crates/docxtpl-pdf`,
  `vendor/docxtplrs`, `vendor/minijinja` and `vendor/zlib-rs`. No Python
  dependency, no C library, no build script, no spawned process. It must build
  for `wasm32-unknown-unknown`.
- `crates/docxtpl-core` is the host core. It may call C libraries directly
  from Rust (libxml2). It must not depend on Python.
- Python exists only in `bindings/python`, `python/`, `tests/` and `scripts/`:
  the binding, its tests and the laboratory.
- `cargo build` and `cargo test` at the root must work with no Python
  installed. Do not add the binding to `default-members`.
- `scripts/check_engine_boundary.sh` enforces this in CI. If it fails, fix the
  dependency; do not relax the script.
- Target architecture: algorithms in Rust; Python only in bindings, tests and
  the laboratory. Calling original Python algorithms from a binding does not
  count as a native port.
- XML and compression may use C libraries called directly from Rust in the
  host core and the binding. Do not route core algorithms through Python
  wrappers for those libraries. Preserve Python object protocols and user
  callbacks at the binding boundary.

## Dependencies and forks

- Reuse suitable Rust libraries before implementing a component; extend
  existing libraries only for missing compatibility behavior and record fork
  provenance.
- A change inside `vendor/` needs its `FORK.md` entry and its hash in
  `UPSTREAM.json`; `python3 scripts/check_vendor.py` must pass.
- Record every new dependency, fork or adapted behavior in `THIRD_PARTY.md`,
  with its license notice in `vendor/notices/` when the license requires one.
- Fix and record native dependency revisions; commit `Cargo.lock`. Builds use
  `--locked`.
- Keep path crates that other crates depend on as plain `rlib` libraries. A
  `cdylib` target makes Cargo write their outputs without a hash, and builds
  with different features then overwrite each other.

## Compatibility and evidence

- A change to coverage or to an execution boundary updates `docs/STATUS.md`
  (state, decisions, debts, what is missing), and `CHANGELOG.md` when users
  are affected. Measurements are reproducible by the commands in
  `CONTRIBUTING.md` and `docs/STATUS.md`; raw results are not versioned.
- Never report reference-vs-reference results as native parity or speedup.
- Never silently fall back to docxtpl rendering when the native engine fails.
- Compare complete document structures and binaries, not just visible text.
- Matching setup failures, missing outputs, unadapted examples, unavailable
  required renderers and unexpected skips must not count as parity passes.
- Treat original API, side effects, types, exceptions and source-loading
  timing as part of the compatibility work. List incomplete features
  explicitly.
- A performance claim names the workload and how it was measured: release
  builds, generation timed apart from validation and rendering, raw samples
  kept.
- Changes to comparators and guard scripts need a mutation test showing the
  mismatch they catch.
- When the engine deliberately differs from the reference, a test pins both
  results and `docs/STATUS.md` gives the reason.

## Quality gates

- Before proposing a change, run the engine gates in
  [CONTRIBUTING.md](CONTRIBUTING.md#the-engine-rust-only), and the binding's
  when the binding or anything it builds on is touched. The suite's accepted
  failures are in `compatibility/known_failures.json`; a change must not add
  one.
- New behavior comes with a test. A bug fix comes with a test that failed
  before it.
- Type hints in new Python code.

## Documentation

- Docs change in the same pull request as the behavior they describe:
  `README.md` for what users see first, `docs/ARCHITECTURE.md` for layers and
  entry points, `CONTRIBUTING.md` for commands, `THIRD_PARTY.md` for
  dependencies, `docs/README.md` when a document is added or removed.
- Everything in the repository is in English: code, comments, tests, data
  descriptions and documentation.
- State limits plainly. Do not describe a path as native, complete or
  compatible beyond what a test shows.

## Releases

- The `release` workflow creates every tag and GitHub release, from a version
  bump and a `CHANGELOG.md` section merged to `main`
  ([CONTRIBUTING.md](CONTRIBUTING.md#releases)). Do not create tags or releases
  by hand.
- Once a release exists, add user-visible changes to the `Unreleased` section
  of `CHANGELOG.md` as you make them.

## Working here

- Coding agents do not start sub-agents unless the person they work for asks.
