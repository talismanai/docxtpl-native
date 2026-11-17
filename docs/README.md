# Documentation

Start with the [project README](../README.md). Four documents cover the rest.

| Document | Language | What it answers |
| --- | --- | --- |
| [API.md](API.md) | English | The Python classes and functions, the qualified template grammar, and where the API differs from docxtpl's |
| [ARCHITECTURE.md](ARCHITECTURE.md) | English | Which crate does what, where Python is allowed, how a render flows, the native bridge, the OPC writer, and how to consume the engine from another workspace |
| [CONTRACT.md](CONTRACT.md) | English | What "compatible with docxtpl" means here: the pinned reference, the three measurements, how a behaviour enters the catalog, the scopes and the pinned visual renderer |
| [STATUS.md](STATUS.md) | English | The state of each area, the decisions behind it, the deliberate differences from docxtpl, the accepted debts and what is still missing |

The host side of the WebAssembly ABI has its own README:
[js/README.md](../js/README.md) covers loading a module, calling its exports,
reading envelopes and answering its font lookups.

Commands to build, test and release are in
[CONTRIBUTING.md](../CONTRIBUTING.md); the rules that apply to people and to
coding agents are in [AGENTS.md](../AGENTS.md). What changed per version is in
[CHANGELOG.md](../CHANGELOG.md).

The contract itself is machine readable, under `compatibility/`:
`contract.json` (the pinned reference versions, the inventory requirements,
the declared deployment targets),
`spec_contracts.json` (the behaviour catalog, its partitions and decisions),
`known_failures.json` (every accepted failure with its cause),
`performance-budget-baseline.json` (the target of the regression budget) and
`renderer-debian13.json` (the pinned visual renderer).

Measurements are not versioned. Every command in CONTRACT.md and STATUS.md
writes its report to `artifacts/`, which git ignores.
