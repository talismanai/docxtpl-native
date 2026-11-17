import assert from "node:assert/strict";
import test from "node:test";

import { readEnvelope } from "../src/envelope.mjs";

const framed = (metadata, body) => {
  const header = new TextEncoder().encode(JSON.stringify(metadata));
  const out = new Uint8Array(4 + header.length + body.length);
  new DataView(out.buffer).setUint32(0, header.length, true);
  out.set(header, 4);
  out.set(body, 4 + header.length);
  return out;
};

test("the metadata comes first and the body is the rest", () => {
  const body = new TextEncoder().encode("%PDF-1.7");
  const { metadata, body: read } = readEnvelope(framed({ pages: 2 }, body));
  assert.deepEqual(metadata, { pages: 2 });
  assert.deepEqual(read, body);
});

test("a body may be empty", () => {
  const { metadata, body } = readEnvelope(framed({ sections: [] }, new Uint8Array()));
  assert.deepEqual(metadata, { sections: [] });
  assert.equal(body.length, 0);
});

test("the body is read from the right offset of a shared buffer", () => {
  // `call` returns a slice of the module's memory; a view with a non-zero
  // byteOffset must be read from its own start, not the buffer's.
  const bytes = framed({ ok: true }, new TextEncoder().encode("body"));
  const shifted = new Uint8Array(bytes.length + 8);
  shifted.set(bytes, 8);
  const view = shifted.subarray(8);
  assert.deepEqual(readEnvelope(view).metadata, { ok: true });
  assert.equal(new TextDecoder().decode(readEnvelope(view).body), "body");
});

test("a result too short to hold a length is refused", () => {
  assert.throws(() => readEnvelope(new Uint8Array([1, 2])), /envelope truncated: 2 bytes/);
});

test("a header longer than the result is refused", () => {
  const bytes = new Uint8Array(8);
  new DataView(bytes.buffer).setUint32(0, 1000, true);
  assert.throws(() => readEnvelope(bytes), /envelope header truncated/);
});
