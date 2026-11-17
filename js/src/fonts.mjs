// Answering a module's font lookups from a catalog.
//
// A module built for a browser carries no fonts. After a conversion it
// reports the lookups that found nothing — per lookup, the family names it
// tried, in order. The host picks a face for the first name it has, registers
// it and converts again. Only the faces a document uses are fetched, and the
// result is the one a fully registered catalog would give.

/** @typedef {{ file: string, family: string, bold: boolean, italic: boolean, weight?: number }} Face */
/** @typedef {{ names: string[], bold: boolean, italic: boolean }} FontRequest */

const isRegularWeight = (face) =>
  face.weight === undefined || (face.weight >= 350 && face.weight <= 750);

/**
 * The catalog face a converter would use for `name`: the exact style, or for
 * a bold/italic request any face of the family, which is the relaxation a
 * converter applies before giving up; a regular-weight face is preferred over
 * a Thin, Light or Black one.
 *
 * @param {Face[]} faces
 * @param {string} name
 * @param {boolean} bold
 * @param {boolean} italic
 * @returns {Face | undefined}
 */
export const pickFace = (faces, name, bold, italic) => {
  const family = name.toLowerCase();
  const styled = (wantBold, wantItalic) =>
    faces
      .filter(
        (face) =>
          face.family.toLowerCase() === family &&
          face.bold === wantBold &&
          face.italic === wantItalic
      )
      .sort((a, b) => Number(!isRegularWeight(a)) - Number(!isRegularWeight(b)))[0];
  const exact = styled(bold, italic);
  if (exact || !(bold || italic)) {
    return exact;
  }
  for (const [wantBold, wantItalic] of [
    [bold, !italic],
    [!bold, italic],
    [!bold, !italic],
  ]) {
    const face = styled(wantBold, wantItalic);
    if (face) {
      return face;
    }
  }
  return undefined;
};

/**
 * The faces to fetch for `requests`: for each lookup, the first family the
 * catalog has.
 *
 * @param {Face[]} faces
 * @param {FontRequest[]} requests
 * @returns {Face[]}
 */
export const facesFor = (faces, requests) => {
  const chosen = new Map();
  for (const request of requests) {
    for (const name of request.names) {
      const face = pickFace(faces, name, request.bold, request.italic);
      if (face) {
        chosen.set(face.file, face);
        break;
      }
    }
  }
  return [...chosen.values()];
};

/**
 * The faces fetched into one module instance, fetched once each.
 *
 * A face that cannot be fetched or registered is not asked for again: the
 * conversion goes on with what the module has, which is what a missing font
 * means anyway.
 */
export class FontLoader {
  #catalog;
  #fetchFace;
  #register;
  #loads = new Map();

  /**
   * @param {object} options
   * @param {() => Promise<Face[]> | Face[]} options.catalog the faces available
   * @param {(face: Face) => Promise<Uint8Array | undefined>} options.fetchFace
   * @param {(font: Uint8Array) => Promise<unknown>} options.register registers one font file
   */
  constructor({ catalog, fetchFace, register }) {
    this.#catalog = catalog;
    this.#fetchFace = fetchFace;
    this.#register = register;
  }

  /** The files fetched into the current instance. */
  get files() {
    return [...this.#loads.keys()];
  }

  /** Forget them, after the instance they were registered into is gone. */
  reset() {
    this.#loads = new Map();
  }

  #load(face) {
    let load = this.#loads.get(face.file);
    if (!load) {
      load = (async () => {
        try {
          const font = await this.#fetchFace(face);
          if (font) {
            await this.#register(font);
          }
        } catch {
          // Not asked for again.
        }
      })();
      this.#loads.set(face.file, load);
    }
    return load;
  }

  /**
   * Start fetching faces by file name, for a caller that knows which ones a
   * document used last time. Unknown names are skipped; this is a hint, and
   * a conversion still fetches whatever it turns out to need.
   *
   * @param {string[]} files
   */
  async preload(files) {
    try {
      const byFile = new Map((await this.#catalog()).map((face) => [face.file, face]));
      for (const file of files) {
        const face = byFile.get(file);
        if (face) {
          void this.#load(face);
        }
      }
    } catch {
      // A hint only.
    }
  }

  /** Wait for every face already on its way. */
  async settle() {
    await Promise.all(this.#loads.values());
  }

  /**
   * Fetch and register the faces for `requests`. Returns whether any of them
   * was new, which is what tells a caller to convert again.
   *
   * @param {FontRequest[]} requests
   * @returns {Promise<boolean>}
   */
  async answer(requests) {
    const catalog = await this.#catalog();
    const missing = facesFor(catalog, requests).filter((face) => !this.#loads.has(face.file));
    if (missing.length === 0) {
      return false;
    }
    await Promise.all(missing.map((face) => this.#load(face)));
    return true;
  }
}

/**
 * Convert, then keep answering the module's font lookups and converting
 * again until it asks for nothing new.
 *
 * A face that is found can lead to further lookups (a fallback chain), so
 * this runs in rounds rather than once.
 *
 * @template T
 * @param {object} options
 * @param {() => Promise<T>} options.convert one conversion
 * @param {() => Promise<FontRequest[]>} options.requests the lookups that found nothing
 * @param {FontLoader} options.loader
 * @param {number} [options.rounds] how many times to answer and retry
 * @returns {Promise<T>}
 */
export const convertWithFontsOnDemand = async ({ convert, requests, loader, rounds = 4 }) => {
  // Faces already on their way (a caller's hint): usually no second round.
  await loader.settle();
  let converted = await convert();
  for (let round = 0; round < rounds; round++) {
    if (!(await loader.answer(await requests()))) {
      break;
    }
    converted = await convert();
  }
  return converted;
};
