# Security

## Reporting a vulnerability

Report it privately through GitHub: open the repository's **Security** tab and
choose **Report a vulnerability**. Do not open a public issue. Include the
revision, a template or input that reproduces the problem (remove private
content first) and what you observed.

Fixes land on `main` and in the next release. Once releases exist, only the
latest one is supported while the project is experimental.

## What to keep in mind when you embed the engine

- **A template is a program.** Jinja tags can loop, allocate and call the
  filters and globals you registered. Render templates from people you do not
  trust only inside limits you control: a time budget, a memory budget (a Web
  Worker or a WebAssembly instance you can discard) and no host function that
  reaches further than the template should.
- **A DOCX is a ZIP of XML.** The engine parses files it is given. Treat a
  crash, a hang or unbounded memory on a crafted file as a vulnerability and
  report it.
- **The engine's WebAssembly module has no imports.** It cannot open files or
  sockets; it sees only the bytes the host passes in, and CI fails a build
  that adds an import (`scripts/check_wasm_imports.mjs`). A module that also
  links `docxtpl-pdf` imports four wasm-bindgen placeholders, nothing else;
  supply them with functions that throw (`bindgenPlaceholders()` in `js/`), so
  the module still reaches nothing of the host.
- **The Python binding runs your Python.** Callbacks, filters and objects in
  the context execute with the privileges of the process, as they do in
  docxtpl.
