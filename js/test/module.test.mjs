import assert from "node:assert/strict";
import test from "node:test";

import { WasmModule, bindgenPlaceholders } from "../src/module.mjs";
import { fakeModule } from "./support.mjs";

const bytes = (text) => new TextEncoder().encode(text);

test("a call hands the inputs over and returns the output", async () => {
  const fake = fakeModule({
    join: ({ read, emit, args }) => {
      const [left, leftLen, right, rightLen] = args;
      emit(new Uint8Array([...read(left, leftLen), ...read(right, rightLen)]));
      return 0;
    },
  });
  const module = new WasmModule({ instantiate: async () => fake.instance });
  const result = await module.call("join", bytes("tem"), bytes("plate"));
  assert.equal(new TextDecoder().decode(result), "template");
  // Both inputs were released, whatever the export did.
  assert.equal(fake.freed.length, 2);
});

test("a call turns a reported failure into the message", async () => {
  const fake = fakeModule({
    refuse: ({ emit }) => {
      emit("template is not a zip");
      return 1;
    },
  });
  const module = new WasmModule({ instantiate: async () => fake.instance });
  await assert.rejects(() => module.call("refuse", bytes("x")), {
    message: "template is not a zip",
  });
  assert.equal(fake.freed.length, 1);
});

test("a json call parses the output", async () => {
  const fake = fakeModule({
    requests: ({ emit }) => {
      emit('[{"names":["Arial"],"bold":false,"italic":false}]');
      return 0;
    },
  });
  const module = new WasmModule({ instantiate: async () => fake.instance });
  assert.deepEqual(await module.callJson("requests"), [
    { names: ["Arial"], bold: false, italic: false },
  ]);
});

test("a numeric call returns what the export returned", async () => {
  const fake = fakeModule({ register_font: () => 3 });
  const module = new WasmModule({ instantiate: async () => fake.instance });
  assert.equal(await module.callNumber("register_font", bytes("ttf")), 3);
});

test("a trapped instance is dropped and the next call builds a new one", async () => {
  let built = 0;
  let resets = 0;
  const module = new WasmModule({
    onReset: () => resets++,
    instantiate: async () => {
      built++;
      return fakeModule({
        trap: () => {
          throw new WebAssembly.RuntimeError("unreachable");
        },
        ok: ({ emit }) => {
          emit("fine");
          return 0;
        },
      }).instance;
    },
  });
  await assert.rejects(() => module.call("trap", bytes("x")), WebAssembly.RuntimeError);
  assert.equal(resets, 1);
  assert.equal(new TextDecoder().decode(await module.call("ok")), "fine");
  assert.equal(built, 2);
});

test("an ordinary error leaves the instance in place", async () => {
  let built = 0;
  const module = new WasmModule({
    instantiate: async () => {
      built++;
      return fakeModule({
        boom: () => {
          throw new TypeError("not a trap");
        },
        ok: ({ emit }) => {
          emit("fine");
          return 0;
        },
      }).instance;
    },
  });
  await assert.rejects(() => module.call("boom"), TypeError);
  await module.call("ok");
  assert.equal(built, 1);
});

test("a missing export is named in the error", async () => {
  const module = new WasmModule({ instantiate: async () => fakeModule().instance });
  await assert.rejects(() => module.call("absent"), {
    message: "the module has no export named absent",
  });
});

test("a module needs somewhere to come from", () => {
  assert.throws(() => new WasmModule({}), {
    message: "a module needs a url or an instantiate function",
  });
});

test("a url is fetched once, with a non-streaming fallback", async () => {
  // A response without `application/wasm`: compileStreaming refuses it and
  // the module falls back to compiling the bytes.
  const wasm = new Uint8Array([0, 0x61, 0x73, 0x6d, 1, 0, 0, 0]);
  let fetched = 0;
  const module = new WasmModule({
    url: "https://example.invalid/pipeline.wasm",
    fetch: async () => {
      fetched++;
      return new Response(wasm, { headers: { "content-type": "application/octet-stream" } });
    },
  });
  const api = await module.exports();
  assert.deepEqual(Object.keys(api), []);
  await module.exports();
  assert.equal(fetched, 1);
});

test("a url that is not there says so", async () => {
  const module = new WasmModule({
    url: "https://example.invalid/pipeline.wasm",
    fetch: async () => new Response("", { status: 404 }),
  });
  await assert.rejects(() => module.exports(), /pipeline\.wasm unavailable \(404\)/);
});

test("a module is instantiated with the imports it was given", async () => {
  const wasm = new Uint8Array([0, 0x61, 0x73, 0x6d, 1, 0, 0, 0]);
  let seen;
  const module = new WasmModule({
    url: "https://example.invalid/pipeline.wasm",
    imports: bindgenPlaceholders(),
    fetch: async () => new Response(wasm, { headers: { "content-type": "application/wasm" } }),
  });
  // WebAssembly ignores imports a module does not declare, so reach for what
  // was handed over rather than for an effect the empty module cannot have.
  seen = bindgenPlaceholders();
  assert.deepEqual(Object.keys(seen).sort(), [
    "__wbindgen_externref_xform__",
    "__wbindgen_placeholder__",
  ]);
  await module.exports();
});

test("a placeholder says so instead of pretending", () => {
  const { __wbindgen_placeholder__ } = bindgenPlaceholders();
  assert.throws(() => __wbindgen_placeholder__.__wbindgen_describe(), {
    message: "wasm-bindgen placeholder __wbindgen_describe was called",
  });
});
