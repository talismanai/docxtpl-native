// Calling a docxtpl WebAssembly module from a host: one instance, compiled
// once, with the allocate/call/read/free dance of the ABI in one place.
//
// A trap (a Rust panic) leaves the module's memory undefined, so an instance
// that trapped is never reused: `call` forgets it and the next call builds a
// fresh one. Whatever the host registered into the old instance — fonts, for
// example — has to be registered again, which is why `reset` is observable.

/** Compile and instantiate from a URL, streaming when the server allows it. */
const fromUrl = (url, fetchImpl, imports) => async () => {
  const response = await fetchImpl(url);
  if (!response.ok) {
    throw new Error(`${url} unavailable (${response.status})`);
  }
  let module;
  try {
    module = await WebAssembly.compileStreaming(response.clone());
  } catch {
    // A server that does not send `application/wasm`.
    module = await WebAssembly.compile(await response.arrayBuffer());
  }
  return WebAssembly.instantiate(module, imports);
};

/**
 * Imports for the wasm-bindgen placeholders a module can carry.
 *
 * A module that reads ZIP archives on wasm32 pulls in `getrandom`, whose
 * backend there talks to the host through wasm-bindgen, and the four
 * placeholders below are what a `wasm-bindgen` post-processing step would
 * normally remove. A module that never asks for randomness never calls them,
 * so these throw rather than return: if one is ever called, it says so here
 * instead of failing somewhere further away.
 */
export const bindgenPlaceholders = () => {
  const placeholder = (name) => () => {
    throw new Error(`wasm-bindgen placeholder ${name} was called`);
  };
  return {
    __wbindgen_externref_xform__: {
      __wbindgen_externref_statics_end: placeholder("__wbindgen_externref_statics_end"),
      __wbindgen_externref_table_grow: placeholder("__wbindgen_externref_table_grow"),
      __wbindgen_externref_table_set_null: placeholder("__wbindgen_externref_table_set_null"),
    },
    __wbindgen_placeholder__: {
      __wbindgen_describe: placeholder("__wbindgen_describe"),
    },
  };
};

export class WasmModule {
  #instantiate;
  #instance;
  #onReset;

  /**
   * @param {object} options
   * @param {string | URL} [options.url] where to fetch the module from
   * @param {() => Promise<WebAssembly.Instance | WebAssembly.WebAssemblyInstantiatedSource>} [options.instantiate]
   *   build the instance yourself (a test, or a module already compiled)
   * @param {typeof fetch} [options.fetch] the fetch to use with `url`
   * @param {() => void} [options.onReset] called when a trapped instance is dropped
   * @param {WebAssembly.Imports} [options.imports] what the module imports;
   *   a module built from this engine imports nothing unless it carries the
   *   PDF converter, which needs [`bindgenPlaceholders`]
   */
  constructor({ url, instantiate, fetch: fetchImpl = globalThis.fetch, onReset, imports = {} } = {}) {
    if (!instantiate && !url) {
      throw new Error("a module needs a url or an instantiate function");
    }
    this.#instantiate = instantiate ?? fromUrl(url, fetchImpl, imports);
    this.#onReset = onReset;
  }

  /** The module's exports, compiling and instantiating on first use. */
  exports() {
    this.#instance ??= (async () => {
      const created = await this.#instantiate();
      return created.exports ?? created.instance.exports;
    })();
    return this.#instance;
  }

  /** Forget the instance; the next call builds a new one. */
  reset() {
    this.#instance = undefined;
    this.#onReset?.();
  }

  /** Copy `bytes` into the module and return the pointer and length. */
  #put(api, bytes) {
    const pointer = api.docxtpl_alloc(bytes.length);
    new Uint8Array(api.memory.buffer, pointer, bytes.length).set(bytes);
    return [pointer, bytes.length];
  }

  async #invoke(name, inputs, read) {
    const api = await this.exports();
    if (typeof api[name] !== "function") {
      throw new Error(`the module has no export named ${name}`);
    }
    const buffers = inputs.map((bytes) => this.#put(api, bytes));
    try {
      return read(api, api[name](...buffers.flat()));
    } catch (error) {
      if (error instanceof WebAssembly.RuntimeError) {
        this.reset();
      }
      throw error;
    } finally {
      // The instance may be gone after a trap; its memory is not ours to free.
      if (this.#instance) {
        for (const [pointer, length] of buffers) {
          api.docxtpl_free(pointer, length);
        }
      }
    }
  }

  /**
   * Call an export that reports a result: 0 with the bytes, or 1 with an
   * error message, in the module's output buffer.
   *
   * @param {string} name
   * @param {...Uint8Array} inputs
   * @returns {Promise<Uint8Array>}
   */
  call(name, ...inputs) {
    return this.#invoke(name, inputs, (api, status) => {
      const bytes = new Uint8Array(
        api.memory.buffer,
        api.docxtpl_output_ptr(),
        api.docxtpl_output_len()
      ).slice();
      if (status !== 0) {
        throw new Error(new TextDecoder().decode(bytes));
      }
      return bytes;
    });
  }

  /** [`call`](#call), with the result decoded as UTF-8 JSON. */
  async callJson(name, ...inputs) {
    return JSON.parse(new TextDecoder().decode(await this.call(name, ...inputs)));
  }

  /**
   * Call an export whose own return value is the answer (a count, a flag),
   * not a status. Its output buffer is not read.
   *
   * @param {string} name
   * @param {...Uint8Array} inputs
   * @returns {Promise<number>}
   */
  callNumber(name, ...inputs) {
    return this.#invoke(name, inputs, (_api, returned) => returned);
  }
}
