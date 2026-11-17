# `@docxtpl/wasm` — the host side of the WebAssembly ABI

A module built from `crates/docxtpl-wasm` (or from a pipeline that embeds it)
exports the raw ABI: the host allocates input buffers with `docxtpl_alloc`,
calls an export, reads the result or the error message through
`docxtpl_output_ptr` and `docxtpl_output_len`, then frees what it allocated.
This package is that dance, written once, with no dependencies.

It is deliberately ignorant of any particular pipeline: you name the exports.

```js
import { WasmModule, readEnvelope } from "@docxtpl/wasm";

const module = new WasmModule({ url: "/pipeline.wasm" });

// An export that returns bytes, or reports an error message.
const docx = await module.call("my_render", template, payload);

// An export that frames more than bytes.
const { metadata, body: pdf } = readEnvelope(await module.call("my_to_pdf", docx));

// An export whose own return value is the answer, not a status.
const faces = await module.callNumber("my_register_font", font);
```

An instance that traps (a Rust panic) is dropped, because its memory is no
longer defined; the next call builds a fresh one.

A module built with the PDF converter is not import-free: `zip` pulls
`getrandom` in on wasm32, and its backend there goes through wasm-bindgen.
Those imports are never called, and `bindgenPlaceholders()` satisfies them
with functions that throw if one ever is:

```js
const module = new WasmModule({ url: "/pipeline.wasm", imports: bindgenPlaceholders() });
``` Anything the host had
registered into it is gone with it, which is what `onReset` is for.

## Fonts on demand

A module built for a browser has no fonts. After a conversion it can report
the font lookups that found nothing — per lookup, the families it tried, in
order. The host picks a face for the first family it has, registers it and
converts again, so only the faces a document uses are fetched and the result
is the one a fully registered catalog would give.

```js
import { FontLoader, convertWithFontsOnDemand } from "@docxtpl/wasm";

const loader = new FontLoader({
  catalog: () => fetchCatalog(),              // [{ file, family, bold, italic, weight? }]
  fetchFace: async (face) => fetchFont(face.file),
  register: (font) => module.callNumber("my_register_font", font),
});

const pdf = await convertWithFontsOnDemand({
  convert: () => module.call("my_to_pdf", docx),
  requests: () => module.callJson("my_font_requests"),
  loader,
});
```

`loader.preload(files)` starts fetching the faces a document needed last
time, so the usual case converts once. After a trap, `loader.reset()` forgets
them along with the instance.

## Tests

```sh
cd js && npm test     # node --test, no dependencies to install
```
