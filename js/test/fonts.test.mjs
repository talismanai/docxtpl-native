import assert from "node:assert/strict";
import test from "node:test";

import {
  FontLoader,
  convertWithFontsOnDemand,
  facesFor,
  pickFace,
} from "../src/fonts.mjs";

const face = (file, family, bold = false, italic = false, weight) => ({
  file,
  family,
  bold,
  italic,
  ...(weight === undefined ? {} : { weight }),
});

const CATALOG = [
  face("arial.ttf", "Arial"),
  face("arial-bold.ttf", "Arial", true),
  face("times.ttf", "Times New Roman"),
  face("thin.ttf", "Narrow", false, false, 100),
  face("regular.ttf", "Narrow", false, false, 400),
];

test("a face is matched by family, ignoring case", () => {
  assert.equal(pickFace(CATALOG, "arial", false, false).file, "arial.ttf");
  assert.equal(pickFace(CATALOG, "Arial", true, false).file, "arial-bold.ttf");
});

test("a family the catalog does not have is not guessed at", () => {
  assert.equal(pickFace(CATALOG, "Calibri", false, false), undefined);
});

test("a style the catalog does not have falls back inside the family", () => {
  // Arial italic is absent: any Arial face is better than no font at all,
  // which is the relaxation the converter itself applies.
  assert.equal(pickFace(CATALOG, "Arial", false, true).file, "arial.ttf");
  // A regular request is not relaxed: there is nothing to relax to.
  assert.equal(pickFace([face("b.ttf", "Solo", true)], "Solo", false, false), undefined);
});

test("a regular weight wins over a thin or black one", () => {
  assert.equal(pickFace(CATALOG, "Narrow", false, false).file, "regular.ttf");
});

test("each lookup takes the first family the catalog has", () => {
  const requests = [
    { names: ["Calibri", "Times New Roman", "Arial"], bold: false, italic: false },
    { names: ["Calibri"], bold: false, italic: false },
  ];
  assert.deepEqual(
    facesFor(CATALOG, requests).map((f) => f.file),
    ["times.ttf"]
  );
});

test("the same face asked for twice is fetched once", () => {
  const requests = [
    { names: ["Arial"], bold: false, italic: false },
    { names: ["Arial"], bold: false, italic: false },
  ];
  assert.equal(facesFor(CATALOG, requests).length, 1);
});

const loaderOver = (catalog, fetchFace) => {
  const registered = [];
  const loader = new FontLoader({
    catalog: () => catalog,
    fetchFace,
    register: async (font) => registered.push(new TextDecoder().decode(font)),
  });
  return { loader, registered };
};

const fetchNamed = (calls) => async (f) => {
  calls.push(f.file);
  return new TextEncoder().encode(f.file);
};

test("answering a lookup fetches and registers the face once", async () => {
  const calls = [];
  const { loader, registered } = loaderOver(CATALOG, fetchNamed(calls));
  const requests = [{ names: ["Arial"], bold: false, italic: false }];
  assert.equal(await loader.answer(requests), true);
  // Asked again, nothing is new, so the caller does not convert again.
  assert.equal(await loader.answer(requests), false);
  assert.deepEqual(calls, ["arial.ttf"]);
  assert.deepEqual(registered, ["arial.ttf"]);
  assert.deepEqual(loader.files, ["arial.ttf"]);
});

test("a face that cannot be fetched is not asked for again", async () => {
  let calls = 0;
  const { loader, registered } = loaderOver(CATALOG, async () => {
    calls++;
    throw new Error("network");
  });
  const requests = [{ names: ["Arial"], bold: false, italic: false }];
  assert.equal(await loader.answer(requests), true);
  assert.equal(await loader.answer(requests), false);
  assert.equal(calls, 1);
  assert.deepEqual(registered, []);
});

test("a preload hint fetches the files it knows and ignores the rest", async () => {
  const calls = [];
  const { loader } = loaderOver(CATALOG, fetchNamed(calls));
  await loader.preload(["times.ttf", "absent.ttf"]);
  await loader.settle();
  assert.deepEqual(calls, ["times.ttf"]);
});

test("a reset forgets the faces of an instance that is gone", async () => {
  const calls = [];
  const { loader } = loaderOver(CATALOG, fetchNamed(calls));
  await loader.answer([{ names: ["Arial"], bold: false, italic: false }]);
  loader.reset();
  assert.deepEqual(loader.files, []);
  await loader.answer([{ names: ["Arial"], bold: false, italic: false }]);
  assert.deepEqual(calls, ["arial.ttf", "arial.ttf"]);
});

test("a conversion runs again once a font arrives, and stops when nothing is missing", async () => {
  const calls = [];
  const { loader } = loaderOver(CATALOG, fetchNamed(calls));
  let conversions = 0;
  const pending = [[{ names: ["Arial"], bold: false, italic: false }], []];
  const converted = await convertWithFontsOnDemand({
    convert: async () => ++conversions,
    requests: async () => pending.shift() ?? [],
    loader,
  });
  assert.equal(conversions, 2);
  assert.equal(converted, 2);
  assert.deepEqual(calls, ["arial.ttf"]);
});

test("a fallback chain is answered in rounds, up to the limit", async () => {
  const calls = [];
  const { loader } = loaderOver(CATALOG, fetchNamed(calls));
  let conversions = 0;
  // Each round reveals one more family: the loop must not stop at the first.
  const pending = [
    [{ names: ["Arial"], bold: false, italic: false }],
    [{ names: ["Times New Roman"], bold: false, italic: false }],
    [{ names: ["Narrow"], bold: false, italic: false }],
  ];
  await convertWithFontsOnDemand({
    convert: async () => ++conversions,
    requests: async () => pending.shift() ?? [],
    loader,
    rounds: 2,
  });
  // The first conversion plus two rounds, and no more.
  assert.equal(conversions, 3);
  assert.deepEqual(calls, ["arial.ttf", "times.ttf"]);
});
