# Status

What the project does today, the decisions that shape it and the reason for
each, and what is still missing. It states no counts that would go stale:
every number comes from a command in [How to check](#how-to-check).

## Where it stands

| Area | State |
| --- | --- |
| Behaviour catalog | Every partition of every catalogued contract is either bound to a differential probe or out of the preserved scope with a recorded decision. None is pending. |
| Unit suite | No pending failure. Every failing test is an [accepted debt](#accepted-debts), and the gate checks each one on every run. |
| Differential corpus | Passes in full with required visual comparison. The only differences from the reference are the [registered divergences](#deliberate-differences-from-docxtpl). |
| Probes | One differential probe per partition, with the reference's observation recorded next to it. |
| API matrix | Every symbol of the inventory is mapped. Every requirement is qualified except `rendering.word_and_real_documents` (see [What is missing](#what-is-missing)). |
| Platforms | Linux arm64 and x86_64, CPython 3.11 and 3.12: the four targets declared in `deployment_targets`, each with a machine-checked installation report. |
| Distribution | A `linux` wheel that links the system libxml2, and a portable `manylinux_2_28` wheel with libxml2 bundled. |

## How to check

```bash
# the whole suite, then the gate on the accepted debts: it rejects a new
# failure, a failure that changed kind, and a debt that started to pass
PYTHONPATH=tests:python python -m unittest discover -s tests -v > suite.log 2>&1
python scripts/check_known_failures.py suite.log

# the product with the reference uninstalled
PYTHONPATH=tests:python python scripts/run_product_tests.py

# the whole differential corpus with visual comparison
# (--jobs N evaluates N cases at a time: same verdict, no timings)
python -m paritylab.cli --suite all --visual required --jobs 6 --out artifacts/parity

# while iterating: only what did not pass in the previous report
# (the report is marked partial and qualifies no revision)
python -m paritylab.cli --suite all --visual required --jobs 6 \
  --rerun-from artifacts/parity/report.json --out artifacts/parity-rerun

# the two gates of the contract
python scripts/compatibility_matrix.py --check
python scripts/spec_test_coverage.py --check
```

The platform requirement takes one installation report per declared target,
produced inside that target from a clean checkout of the revision under test:

```bash
scripts/qualify_deployment_target.sh 3.12 reports/linux-x86_64-cp312.json
```

The four reports enter the matrix together:

```bash
python scripts/compatibility_matrix.py --check \
  --deployment-report reports/linux-x86_64-cp311.json \
  --deployment-report reports/linux-x86_64-cp312.json \
  --deployment-report reports/linux-aarch64-cp311.json \
  --deployment-report reports/linux-aarch64-cp312.json
```

CI qualifies every change this way: the `deployment-target` job of
[`ci.yml`](../.github/workflows/ci.yml) runs the script on a runner of each
target's architecture, and the `contract` job runs the two gates over those
reports and the merged 3.12 corpus. On CPython 3.11
the accepted-debt map does not apply as such (it is calibrated on 3.12): the
check is that no test fails there that does not fail on 3.12, which is what
`scripts/check_known_failures.py` enforces for a log of another version.

## Decisions

These hold for every change. Each one exists to keep a claim of compatibility
from being weaker than it looks.

- **Explicit failure, never a fallback.** An unsupported feature raises
  `UnsupportedFeature`. No path falls back to `docxtpl.DocxTemplate.render` or
  to a Jinja2 render, so a passing render is always the engine's render.
- **Evidence belongs to one revision.** A report from another revision, two
  reports for one target and a report for an undeclared target are issues of
  the matrix: they would claim evidence that does not exist. A target without
  a report stays `unverified` rather than failing, because one workstation or
  one CI leg can only prove its own platform.
- **A divergence is exact.** A case counts as a registered divergence only when
  it differs exactly as declared in `python/paritylab/divergences.py`; any
  other difference is a gap. A divergence never qualifies a requirement.
- **The checker is not weakened to pass.** A change of scope needs a recorded
  decision; a change to a comparator or a gate needs a mutation test showing
  what it catches. A test that covers changed code is neither adapted nor
  skipped: the engine is what gets corrected.
- **Internal pipeline steps** (`fix_tables`, `map_tree`, `build_xml` and the
  other steps of `DocxTemplate` and `SubdocComposer`): what is preserved is
  *that* an invalid argument raises, not which error. Their other partitions
  are out of scope, each with its decision, because they describe docxtpl's
  implementation rather than its contract.
- **What differential probes do not measure.** Performance, native ownership
  and installation are properties of the package, not behaviours a template
  shows. Their partitions point at what measures them: the per-workload
  regression budget, the native-execution audits and the installation reports
  of the declared targets.
- **Inventory scope.** The API of docxtpl and the public part of Jinja2. The
  Jinja2 compiler is out, since the engine replaces it; bytecode caches are
  refused; of python-docx, lxml and docxcompose only the classes this package
  hands out are in, because the rest of their API is theirs.
- **Vendored forks** (`vendor/minijinja`, `vendor/docxtplrs`, `vendor/zlib-rs`):
  every local change is described in the fork's `FORK.md` and recorded by
  hash in its `UPSTREAM.json`, so a reader can tell upstream code from ours
  and `scripts/check_vendor.py` can reject an unrecorded change.

## Deliberate differences from docxtpl

Three behaviours of the reference are not reproduced, because reproducing
them would damage the document. Each one is pinned by a test that renders
with both engines and asserts both results.

| Behaviour | docxtpl | Here | Why | Pinned by |
| --- | --- | --- | --- | --- |
| `-%}` merging with the next text | Deletes the markup up to the next `<w:t`, and its pattern also stops at `<w:tabs>`, `<w:tbl>`, `<w:tr>` and `<w:tc>` | Never deletes across a table, row or cell boundary, nor tab stops | Deleting that markup leaves the document structure unbalanced | `tests/test_deliberate_differences.py` |
| Ill-formed text in the body (a stray `&` or `<`, entities after the first error) | libxml2's recovery drops it | Kept | Losing the user's content is worse than differing from the reference | `tests/test_deliberate_differences.py`; corpus case `protocol/unescaped_body_recovery` |
| `w:nsid` of a copied list | Drawn from Python's `random` in docxcompose | Derived from the list definition | The same input yields the same bytes | `tests/test_native_deferred_marker.py`; corpus case `protocol/composer_add_numberings` |

### Namespace binding limit

The namespace-aware XML readers keep quick-xml 0.42's default limit of 128
simultaneous namespace bindings, excluding the built-in `xml` and `xmlns`
bindings. Declarations inherited from ancestors count toward the limit;
bindings leave scope when their element closes. Exceeding the limit rejects
the document instead of allowing unbounded namespace allocation
(RUSTSEC-2026-0195). This is a deliberate security boundary: the previous
quick-xml 0.36 readers had no such limit. It does not change the public ABI.

`cargo test -p docxtpl-markers --release --locked namespace_bindings` checks
the boundary at 128 and 129 bindings, including inherited and closed scopes.
`tests/test_deliberate_differences.py` renders documents at both sizes with
both engines: docxtpl accepts both, while this engine accepts 128 and rejects
129.

## API differences

The classes are this package's own and docxtpl is not a dependency. What that
changes for code written against docxtpl, and why, is in
[API.md](API.md#divergences).

## Accepted debts

An accepted debt is a difference kept on purpose. It is not a tolerance: the
test keeps failing, always the same way, and
`scripts/check_known_failures.py` rejects a run in which the failure changes
kind, a new one appears, or a debt starts to pass without a decision. The
tests and their causes are listed in
[`compatibility/known_failures.json`](../compatibility/known_failures.json).

Almost all share one root: templates run in a native VM, not as Python code
generated by Jinja. Code that inspects the generated code, the code object of a
coroutine, reference counts or CPython frames sees something else, and
reproducing it would mean generating Python code, which is what the engine
exists to avoid.

| Cause | Why it is accepted | Contracts |
| --- | --- | --- |
| `async_throw_protocol` | `send`, `throw` and `close` on an async render do not delegate as the reference does: async generator hooks, the per-layer warnings of three-argument `throw`, reentrancy checks and unraisable-exception reporting. The native render is one coroutine, not a stack of generators. | `jinja.async.callback` |
| `cpython_specialization_error` | The text of the `SystemError` raised for an invalid escape result depends on the specialisation of the call site and on CPython's profiling state; the native engine reports the unspecialised message. | `jinja.builtin.filter.forceescape` |
| `join_generator_identity` | A user `join` observes a different generator (reference count, code object or caller frame) from the one `indent` passes in the reference. | `jinja.builtin.filter.indent`, `jinja.builtin.filter.join` |
| `from_code_hook` | A replaced `Template.from_code` is not called: the native compiler builds the template without it, so its builtins and side effects do not appear. | `jinja.extensions.ast` |
| `import_cache_semantics` | `Environment.cache` is not called: Jinja stores compiled Python `Template` objects in it, which the native compiler does not produce. The reload rules are followed natively. | `jinja.language.import` |
| `async_iterator_code_clone` | An exact clone of the code of `AsyncLoopContext._to_iterator`, installed before the extension loads, cannot be told from the original and runs natively. | `jinja.async.callback`, `jinja.iteration.lookahead` |
| `async_iterator_reference_count` | A replaced async loop factory observes a different reference count during the call. | `jinja.async.callback`, `jinja.iteration.lookahead` |
| `block_reference_adapter` | Calling `self.<block>` with the names the compiler adds inside loops (`_loop_vars`) raises `SyntaxError` in the reference's generated Python code; the native template has no generated code. | `jinja.language.super`, `jinja.sandbox.call` |
| `escape_code_identity` | An exact clone of the code of `markupsafe.escape` is treated as the standard algorithm, not as a user callback. | `jinja.builtin.filter.forceescape` |
| `generated_code_identity` | The awaitables of a recursive loop and of `super()` do not expose the name or the code object of the reference's generated coroutine. | `jinja.async.callback` |
| `generated_module_lifetime` | A replaced `auto_aiter`, imported by Jinja's generated module, lives until that module's cycle is collected; the native render releases it at the end of the render. | `jinja.async.callback`, `jinja.iteration.lookahead` |
| `join_getter_protocol` | The attribute getter `join` passes is a native object, not a function, and the async adapter runs its Python body. | `jinja.builtin.filter.join` |
| `tojson_python_algorithm` | `tojson` runs Jinja's `do_tojson` and `htmlsafe_json_dumps`. A test requires those Python frames in the traceback, which a native port would remove. | `jinja.builtin.filter.tojson` |
| `exported_mapping_frame` | A mapping used as `Context.vars` is read by the native body, not through `get_exported`, so a user `__getitem__` sees its own caller's frame where the reference shows `get_exported`. Matching it would mean calling Jinja's `get_exported`, which the native-execution audit of this area forbids. | `jinja.language.import_context` |
| `loop_snapshot_not_live` | Two requirements contradict each other: the `repr` of an async loop must not run `AsyncLoopContext.__repr__` in Python, and the warning of its un-awaited `length` coroutine must point inside that same `__repr__`. | `jinja.callback.identity`, `jinja.iteration.lookahead` |
| `module_reference_count` | A failing import holds one reference fewer to its target during the callback. The reference keeps it alive in the frame of the Python code it generates; the native VM has no such frame, and matching the count would mean holding an artificial reference for the count alone. | `jinja.language.from_import` |

## What still runs Python

Algorithms belong in Rust and Python belongs at the boundary. These paths
still run Python code:

- `tojson` runs Jinja's Python body (an accepted debt above).
- The programmatic `Subdoc` and picture construction use python-docx and
  docxcompose objects, around a native template execution.
- Constant evaluation and syntax validation happen at compile time with the
  parser of the language the user configured; execution is native. Outside the
  closed grammar ([API.md](API.md#templates-parsed-without-jinja2s-parser)),
  Jinja's parser stays on the path.
- `save` of the exposed document model takes a python-docx snapshot before the
  native OPC writer applies the package rules.

## What is missing

### Rendering in Word and on real documents

`rendering.word_and_real_documents` is the one requirement that does not
qualify, because it names no test: its evidence is opening the output in
Microsoft Word and running the corpus over a collection of real documents.
It and the two catalog contracts that depend on it are marked
`"evidence": "manual"`: no automated run can produce that evidence, so both
gates list them as pending instead of failing, and the matrix stays short of
`fully_compatible` until it exists. The visual comparison that exists
measures fidelity under LibreOffice, not equivalence in Word.

No free tool answers the question, since the repair prompt is Word's own.
Validating against the ECMA-376 schemas would answer another one — whether the
package follows the schema — and still prove nothing about Word, while the
XML equivalence with the reference that the corpus measures already covers more
of that risk.

The path for real documents is in place; only the documents are missing.
`--real-manifest` has a differential test with a generated document
(`tests/test_cli.py`, `RealManifestTests`): each case enters as `real/<id>`, is
compared like any other, and a document the reference cannot open becomes a
`reference_error`, never a parity verdict. Copy `fixtures/real.example.json`,
point it at files in `fixtures/private/` (ignored by git) and run:

```bash
python -m paritylab.cli --real-manifest fixtures/real.json --visual required
```

### Architectural boundaries

The boundaries the crates do not draw yet, and what each one needs before it
is drawn, are in
[ARCHITECTURE.md](ARCHITECTURE.md#boundaries-still-to-establish).

### Accepted debts

Any of the [accepted debts](#accepted-debts) can be taken on if it becomes
worth its cost, which the table states; several would mean generating Python
code.
