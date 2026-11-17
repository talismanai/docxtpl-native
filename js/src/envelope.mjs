/**
 * An export that returns more than bytes frames them: a 4-byte
 * little-endian length, that many bytes of UTF-8 JSON, then the body.
 *
 * @param {Uint8Array} bytes one export's output
 * @returns {{ metadata: unknown, body: Uint8Array }}
 */
export const readEnvelope = (bytes) => {
  if (bytes.length < 4) {
    throw new Error(`envelope truncated: ${bytes.length} bytes`);
  }
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const size = view.getUint32(0, true);
  if (bytes.length < 4 + size) {
    throw new Error(`envelope header truncated: ${size} of ${bytes.length - 4} bytes`);
  }
  return {
    body: bytes.slice(4 + size),
    metadata: JSON.parse(new TextDecoder().decode(bytes.subarray(4, 4 + size))),
  };
};
