import assert from "node:assert/strict";
import test from "node:test";

import { BINDGEN_PLACEHOLDERS, unexpectedImports } from "./check_wasm_imports.mjs";

// A module with no sections at all.
const EMPTY = new Uint8Array([0x00, 0x61, 0x73, 0x6d, 0x01, 0x00, 0x00, 0x00]);

/** A module importing one function, `module::name`. */
const importing = (module, name) => {
  const text = (s) => [s.length, ...new TextEncoder().encode(s)];
  const entry = [0x01, ...text(module), ...text(name), 0x00, 0x00];
  return new Uint8Array([
    ...EMPTY,
    0x01, 0x04, 0x01, 0x60, 0x00, 0x00, // type section: () -> ()
    0x02, entry.length, ...entry, // import section
  ]);
};

test("a module without imports passes with nothing allowed", () => {
  assert.deepEqual(unexpectedImports(EMPTY), []);
});

test("an import is reported when nothing is allowed", () => {
  assert.deepEqual(unexpectedImports(importing("env", "open")), ["env::open"]);
});

test("the placeholders pass only when they are allowed", () => {
  const [module, name] = BINDGEN_PLACEHOLDERS[0].split("::");
  const bytes = importing(module, name);
  assert.deepEqual(unexpectedImports(bytes), [BINDGEN_PLACEHOLDERS[0]]);
  assert.deepEqual(unexpectedImports(bytes, BINDGEN_PLACEHOLDERS), []);
});

test("allowing the placeholders does not let anything else through", () => {
  assert.deepEqual(
    unexpectedImports(importing("env", "open"), BINDGEN_PLACEHOLDERS),
    ["env::open"]
  );
});
