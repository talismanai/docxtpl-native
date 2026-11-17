# The compatibility contract

This package renders the same documents as [docxtpl](https://github.com/elapouya/python-docx-template)
without depending on it at run time. "The same" is not an opinion here: it is a
contract with a pinned reference, executable comparisons and recorded
decisions. This page is that contract — what is compared, how, and what is
deliberately different.

The target versions are in [compatibility/contract.json](../compatibility/contract.json):
docxtpl 0.20.2 with Jinja2 3.1.6, python-docx 1.2.0, lxml 6.1.1 and
docxcompose 1.4.0, on CPython 3.11 and 3.12. Upgrading any of them needs its
own measurement. The reference is installed only from its pinned checkout
(`scripts/fetch_upstream.py`), never from the package index: the same version
number on PyPI does not prove the same source.

The Python API is the package's own ([API.md](API.md)). Where it differs from
docxtpl on purpose, the difference is listed in the catalog with the tests that
pin it, and in [STATUS.md](STATUS.md#deliberate-differences-from-docxtpl) with
the reason.

## Three separate measurements

1. **Document parity** compares the produced `.docx`: text in every story,
   complete namespace-aware XML of every part, attribute order, binary parts
   byte for byte, OPC validity, compressed and uncompressed size, and the
   rendered pixels.
2. **Protocol parity** compares what happens *during* a render: the arguments a
   callback receives, the identity of the Python objects it sees, mutations,
   iteration and exception order, loader and `Environment` behaviour,
   reentrancy, and the types and references the API returns.
3. **API traceability** inventories the reference's classes, members,
   signatures, protocols and Jinja builtins, and binds each one to a
   requirement. A binding is traceability for review, never proof.

Two documents that match cannot compensate for different side effects, and a
matching infrastructure failure is never a pass: a reference that fails its own
oracle is a `reference_error`. Timings and sizes are recorded as separate
measurements and never turn into speedup claims.

```bash
python -m paritylab.cli --suite all --visual required --out artifacts/parity
python scripts/compatibility_matrix.py --check
python scripts/spec_test_coverage.py --check
```

Cases are independent, so `--jobs N` evaluates N of them at a time (each case
still runs both engines in their own processes, and each conversion gets its
own LibreOffice profile). The verdict is identical and the order is kept; what
a concurrent run does not produce is timings, because a loaded host measures
nothing comparable — `--repeat`/`--warmup` therefore refuse to run with
`--jobs` above one. For a single case while developing, `--case` and
`--protocol-case` select it, and the report records what was selected: a
partial run qualifies nothing, because a selection that covers exactly the
tests the contract names would qualify the matrix while most of the corpus
never ran.

## How a behaviour enters the catalog

[compatibility/spec_contracts.json](../compatibility/spec_contracts.json) holds
263 contracts. Each one is built the same way:

1. Fix the reference revision, link the contract to the upstream source it
   describes and give it a stable id.
2. Split the operation by **observable behaviour**: normal result, boundaries,
   invalid input, exceptions, identity and mutation, lifecycle order,
   interactions. For documents, also parts, relationships, formatting and
   validity.
3. Write one independent probe per partition and record the reference's
   observation (`scripts/record_reference_observations.py`). Interactions
   count; isolated features alone do not.
4. Run the same case against the pinned reference and the engine, in separate
   processes, and compare everything in measurement 1 and 2.
5. A pass means those cases passed — nothing more. `reviewed: true` is a human
   statement that the partitions, failure paths and sources were assessed.

Today every partition is bound to a probe: 1,259 differential partition probes
(`python/paritylab/*_partition_probes.py`, with their recorded reference
observations) and no pending partition. Two contracts have no case at all,
because their evidence is the owner's: visual output in Microsoft Word and a
private corpus of real documents.

## Scopes, divergences and debts

Each contract has a `scope`:

| Scope | Meaning |
| --- | --- |
| `preserved` | Compared with the reference; the only scope that can qualify |
| `divergence` | Differs on purpose, with a recorded `scope_decision` |
| `out_of_scope` | Outside the preserved contract, with a recorded decision |

A partition can leave the preserved scope only with its reason in
`partition_decisions`; `scripts/spec_test_coverage.py` fails on an out-of-scope
partition without one. 231 partitions are out of scope today: the internal
render steps of `DocxTemplate` and `SubdocComposer` preserve only *that* an
error happens (`python/paritylab/internal_error_probes.py`), and the rest are not
differential behaviour at all (performance, native-execution audits,
packaging). Everything users call keeps every partition.

Two kinds of known difference exist, and neither is ever counted as parity:

- **Accepted divergences of output or API** — listed under `divergences` in the
  catalog, each with the tests that pin it, and explained in
  [STATUS.md](STATUS.md). Two of them show up in the document corpus; a
  divergent case counts as a divergence only when it differs *exactly* as
  declared (`python/paritylab/divergences.py`).
- **Known failures of the test suite** —
  [compatibility/known_failures.json](../compatibility/known_failures.json)
  lists every one with its cause and the contracts it belongs to.
  `scripts/check_known_failures.py` rejects a run with a new failure, a changed
  failure kind, or a known failure that now passes.

## Test tiers

`scripts/run_product_tests.py --list` splits the suite by what each module may
touch:

- **Differential**: reaches the pinned docxtpl, by import or by name.
- **Product**: runs with every import of docxtpl blocked and recorded — the
  product must work without the reference installed.
- **Native execution audit**: imports `tests/native_audit.py` and proves *which
  code ran*; that its output equals the reference is a separate differential
  test.

## The visual comparison

Rendered pixels come from an independent pipeline: LibreOffice converts both
documents to PDF and PyMuPDF rasterizes them at 108 DPI, and the comparator
requires exact pixels. This is LibreOffice fidelity, not Word equivalence.

The renderer is pinned, because a different build produces different pixels for
the same file: the profile is Debian 13 / x86_64 with LibreOffice 25.2.3.2, and
`scripts/install_lab_renderer.py` verifies every package's SHA-256 against
[compatibility/renderer-debian13.json](../compatibility/renderer-debian13.json)
before extracting it into a local prefix. The laboratory's own stability is
measured separately, by converting the same file repeatedly and by mutating
text and formatting to prove the comparator notices:

```bash
python scripts/install_lab_renderer.py --prefix work/renderer
export PATH="$PWD/work/renderer/bin:$PATH"
python -m paritylab.visual_selfcheck --document fixtures/<file>.docx --pairs 5 \
  --out artifacts/renderer-repeat.json
```

## What blocks certification

`scripts/compatibility_matrix.py --check` qualifies a requirement only when its
named tests pass **in a differential report produced at the same commit** and
the requirement is `reviewed`. Self-checks, a report from another revision and
a stale artifact qualify nothing.

A report names its commit from `GITHUB_SHA` in CI, or from the checkout when
`git status` is clean — so a run in a container or on a workstation can qualify
a revision too. A run with uncommitted changes names no revision at all, and
the matrix says so: the code that ran is not the commit it started from.

The report also has to name one of the declared interpreters: a corpus run on
a Python the contract does not promise measures something else.

Installing on a platform is not a differential case, so it cannot come out of
the corpus. `deployment.python_platform_matrix` names one id per target under
`deployment_targets` in the contract (`deployment/linux_x86_64_cp311` and so
on), and a target qualifies only from the wheel installation report that
`python/paritylab/check_wheel_install.py` writes **inside that target** — the
fresh environment, the pinned reference, the wheel tag and the rendered text
are all checked field by field (`python/paritylab/standalone_evidence.py`).
`scripts/qualify_deployment_target.sh <python> <report>` produces one report
in a clean checkout of the revision; the four go into the matrix together:

```bash
python scripts/compatibility_matrix.py --check \
  --deployment-report reports/linux-x86_64-cp311.json \
  --deployment-report reports/linux-x86_64-cp312.json \
  --deployment-report reports/linux-aarch64-cp311.json \
  --deployment-report reports/linux-aarch64-cp312.json
```

A report from another revision, two reports for one target and a target the
contract does not declare are issues: they claim evidence that is not there. A
declared target with no report stays unverified without an issue, because a
workstation has no containers. CI collects all four on every change: the
`deployment-target` job of [`ci.yml`](../.github/workflows/ci.yml) runs the
script on a runner of each target's architecture, and the `contract` job runs
both gates over those reports and the corpus of the 3.12 leg.

A requirement or catalog contract marked `"evidence": "manual"` names no test:
its evidence is a person's check that no automated run produces, such as
opening the output in Microsoft Word. Both gates list it as pending and do not
fail on it, and the matrix stays short of `fully_compatible` until that
evidence exists.

No partition and no known failure is pending, the full corpus passes with
required visual comparison, and every declared platform target has its
installation report. Every requirement and contract is marked `reviewed`. What
is missing is in [STATUS.md](STATUS.md#what-is-missing): rendering in Word and
on real documents.
