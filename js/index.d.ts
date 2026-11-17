/** A face in the font catalog a host can fetch from. */
export type Face = {
  file: string;
  family: string;
  bold: boolean;
  italic: boolean;
  weight?: number;
};

/** A font lookup that found nothing: the families tried, in order. */
export type FontRequest = { names: string[]; bold: boolean; italic: boolean };

/** Read an export's framed result: UTF-8 JSON metadata, then the body. */
export function readEnvelope(bytes: Uint8Array): {
  metadata: unknown;
  body: Uint8Array;
};

/** The face a converter would use for `name`, or nothing if the catalog has none. */
export function pickFace(
  faces: Face[],
  name: string,
  bold: boolean,
  italic: boolean
): Face | undefined;

/** The faces to fetch for `requests`: per lookup, the first family the catalog has. */
export function facesFor(faces: Face[], requests: FontRequest[]): Face[];

/** The faces fetched into one module instance, fetched once each. */
export class FontLoader {
  constructor(options: {
    catalog: () => Promise<Face[]> | Face[];
    fetchFace: (face: Face) => Promise<Uint8Array | undefined>;
    register: (font: Uint8Array) => Promise<unknown>;
  });
  /** The files fetched into the current instance. */
  readonly files: string[];
  /** Forget them, after the instance they were registered into is gone. */
  reset(): void;
  /** Start fetching faces by file name; unknown names are skipped. */
  preload(files: string[]): Promise<void>;
  /** Wait for every face already on its way. */
  settle(): Promise<void>;
  /** Fetch the faces for `requests`; true when any of them was new. */
  answer(requests: FontRequest[]): Promise<boolean>;
}

/** Convert, answering the module's font lookups and retrying, in rounds. */
export function convertWithFontsOnDemand<T>(options: {
  convert: () => Promise<T>;
  requests: () => Promise<FontRequest[]>;
  loader: FontLoader;
  rounds?: number;
}): Promise<T>;

/**
 * Imports for the wasm-bindgen placeholders a module built with the PDF
 * converter carries. They are never called; each throws if it ever is.
 */
export function bindgenPlaceholders(): WebAssembly.Imports;

/** One instance of a docxtpl WebAssembly module, and the ABI around it. */
export class WasmModule {
  constructor(options: {
    url?: string | URL;
    instantiate?: () => Promise<
      WebAssembly.Instance | WebAssembly.WebAssemblyInstantiatedSource
    >;
    fetch?: typeof fetch;
    onReset?: () => void;
    imports?: WebAssembly.Imports;
  });
  /** The module's exports, compiling and instantiating on first use. */
  exports(): Promise<Record<string, unknown> & { memory: WebAssembly.Memory }>;
  /** Forget the instance; the next call builds a new one. */
  reset(): void;
  /** Call an export that reports 0 with bytes, or 1 with an error message. */
  call(name: string, ...inputs: Uint8Array[]): Promise<Uint8Array>;
  /** `call`, with the result decoded as UTF-8 JSON. */
  callJson<T = unknown>(name: string, ...inputs: Uint8Array[]): Promise<T>;
  /** Call an export whose own return value is the answer, not a status. */
  callNumber(name: string, ...inputs: Uint8Array[]): Promise<number>;
}
