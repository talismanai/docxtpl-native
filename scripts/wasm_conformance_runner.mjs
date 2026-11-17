// Run conformance cases through the wasm module: stdin is the case list,
// stdout one {ok, output} result per case. Usage: node runner.mjs module.wasm
import { readFileSync } from "node:fs";

const { instance } = await WebAssembly.instantiate(readFileSync(process.argv[2]), {});
const api = instance.exports;
const encoder = new TextEncoder();
const decoder = new TextDecoder();
function put(text) {
  const bytes = encoder.encode(text);
  const pointer = api.docxtpl_alloc(bytes.length);
  new Uint8Array(api.memory.buffer, pointer, bytes.length).set(bytes);
  return [pointer, bytes.length];
}
const cases = JSON.parse(readFileSync(0, "utf8"));
const results = cases.map(({ template, context }) => {
  const [source, sourceLength] = put(template);
  const [data, dataLength] = put(JSON.stringify(context));
  const status = api.docxtpl_render_string(source, sourceLength, data, dataLength);
  const output = decoder.decode(new Uint8Array(api.memory.buffer, api.docxtpl_output_ptr(), api.docxtpl_output_len()));
  api.docxtpl_free(source, sourceLength);
  api.docxtpl_free(data, dataLength);
  return { ok: status === 0, output };
});
process.stdout.write(JSON.stringify(results));
