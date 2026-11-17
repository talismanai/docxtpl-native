// A stand-in for a compiled module: real linear memory, the ABI's four
// housekeeping exports, and whatever exports a test needs on top.
export const fakeModule = (exports = {}) => {
  const memory = new WebAssembly.Memory({ initial: 2 });
  const freed = [];
  let next = 1024;
  let output = new Uint8Array();
  let outputPointer = 0;

  const read = (pointer, length) =>
    new Uint8Array(memory.buffer, pointer, length).slice();
  const emit = (bytes) => {
    output = bytes instanceof Uint8Array ? bytes : new TextEncoder().encode(bytes);
    outputPointer = next;
    next += Math.max(output.length, 1);
    new Uint8Array(memory.buffer, outputPointer, output.length).set(output);
  };

  const api = {
    memory,
    docxtpl_alloc(length) {
      const pointer = next;
      next += Math.max(length, 1);
      return pointer;
    },
    docxtpl_free(pointer, length) {
      freed.push([pointer, length]);
    },
    docxtpl_output_ptr: () => outputPointer,
    docxtpl_output_len: () => output.length,
  };
  for (const [name, handler] of Object.entries(exports)) {
    api[name] = (...args) => handler({ read, emit, args });
  }
  return { instance: { exports: api }, freed };
};
