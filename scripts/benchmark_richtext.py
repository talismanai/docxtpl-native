"""Measure equivalent RichText construction, preserving raw timings."""
import argparse
import json
import hashlib
import os
import platform
import statistics
import sys
from pathlib import Path
from time import perf_counter

import docxtpl
import docxtpl_native

class CustomFont(str):
    pass


CASES = {
    "plain": {},
    "bold_color": {"bold": True, "color": "123456"},
    "combined_styles": {"style": "Strong", "bold": True, "italic": True,
                        "color": "123456", "size": 18, "underline": "wave"},
    "font": {"font": "Arial", "bold": True},
    "font_subclass": {"font": CustomFont("Arial"), "bold": True},
}


def construct(cls, count, kwargs):
    return [cls("Rich text <&>", **kwargs) for _ in range(count)]


def measure(kwargs, count, repeat):
    reference = construct(docxtpl.RichText, count, kwargs)
    candidate = construct(docxtpl_native.RichText, count, kwargs)
    if [str(value) for value in reference] != [str(value) for value in candidate]:
        raise AssertionError("RichText XML differs from the pinned reference")
    del reference, candidate
    construct(docxtpl.RichText, count, kwargs)
    construct(docxtpl_native.RichText, count, kwargs)

    samples = {"reference_seconds": [], "native_seconds": []}
    implementations = [(docxtpl.RichText, "reference_seconds"),
                       (docxtpl_native.RichText, "native_seconds")]
    for iteration in range(repeat):
        for cls, key in (implementations if iteration % 2 == 0 else reversed(implementations)):
            start = perf_counter()
            values = construct(cls, count, kwargs)
            samples[key].append(perf_counter() - start)
            del values
    ref = statistics.median(samples["reference_seconds"])
    native = statistics.median(samples["native_seconds"])
    return {**samples, "reference_median_seconds": ref,
            "native_median_seconds": native,
            "speedup_reference_over_native": ref / native}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--count", type=int, default=3000)
    parser.add_argument("--repeat", type=int, default=9)
    args = parser.parse_args()
    if args.count < 1 or args.repeat < 1:
        parser.error("count and repeat must be positive")
    native_path=Path(docxtpl_native._extension().__file__)
    report = {"count": args.count, "repeat": args.repeat,
              "source_revision":os.environ.get('GITHUB_SHA'),
              "native_so_sha256":hashlib.sha256(native_path.read_bytes()).hexdigest(),
              "scope":"Warm RichText construction; exact XML checked before timing; imports excluded",
              "python": sys.version, "platform": platform.platform(),
              "cases": {name: measure(kwargs, args.count, args.repeat)
                        for name, kwargs in CASES.items()}}
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
