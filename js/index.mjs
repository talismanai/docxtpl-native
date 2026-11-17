// The host side of the docxtpl WebAssembly ABI. See README.md.
export { readEnvelope } from "./src/envelope.mjs";
export { FontLoader, convertWithFontsOnDemand, facesFor, pickFace } from "./src/fonts.mjs";
export { WasmModule, bindgenPlaceholders } from "./src/module.mjs";
