#!/usr/bin/env node
// Check what a WebAssembly module imports from its host.
//
//   node scripts/check_wasm_imports.mjs MODULE.wasm
//   node scripts/check_wasm_imports.mjs MODULE.wasm --allow-bindgen-placeholders
//
// The engine module (docxtpl-wasm) imports nothing: a host instantiates it with
// an empty import object, and it can reach nothing but the bytes it is given.
// A module that links docxtpl-pdf also imports four wasm-bindgen placeholders,
// which arrive with `zip` on wasm32 and are never called; the flag allows
// exactly those. Anything else is a new capability the host would have to
// grant, so it fails the check.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

export const BINDGEN_PLACEHOLDERS = [
  "__wbindgen_externref_xform__::__wbindgen_externref_statics_end",
  "__wbindgen_externref_xform__::__wbindgen_externref_table_grow",
  "__wbindgen_externref_xform__::__wbindgen_externref_table_set_null",
  "__wbindgen_placeholder__::__wbindgen_describe",
];

/** The imports of `bytes` that are not in `allowed`, as `module::name`. */
export const unexpectedImports = (bytes, allowed = []) => {
  const permitted = new Set(allowed);
  return WebAssembly.Module.imports(new WebAssembly.Module(bytes))
    .map((entry) => `${entry.module}::${entry.name}`)
    .filter((name) => !permitted.has(name));
};

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [path, flag] = process.argv.slice(2);
  if (!path) {
    console.error("usage: check_wasm_imports.mjs MODULE.wasm [--allow-bindgen-placeholders]");
    process.exit(2);
  }
  const allowed = flag === "--allow-bindgen-placeholders" ? BINDGEN_PLACEHOLDERS : [];
  const unexpected = unexpectedImports(readFileSync(path), allowed);
  if (unexpected.length > 0) {
    console.error(`${path} imports what its host is not expected to provide:`);
    for (const name of unexpected) console.error(`  ${name}`);
    process.exit(1);
  }
  console.log(`${path}: no unexpected imports`);
}
