"""Compare Listing construction after checking exact XML values."""
import argparse
import json
import platform
import statistics
import sys
from pathlib import Path
from time import perf_counter

import docxtpl
import docxtpl_native


class CustomText(str):
    def replace(self, old, new):
        return super().replace(old, new)


CASES = {
    "plain": "Hello world",
    "escaped": "a<&>'\\\"",
    "long": "Hello<&> " * 500,
    "integer": 1234,
    "surrogate": chr(0xD800) + "<&",
    "str_subclass": CustomText("<&>"),
}


def measure(value, count, repeat):
    reference = docxtpl.Listing(value).xml
    candidate = docxtpl_native.Listing(value).xml
    if candidate != reference:
        raise AssertionError("Listing XML differs from the reference")
    implementations = [(docxtpl.Listing, "reference_seconds"),
                       (docxtpl_native.Listing, "native_seconds")]
    samples = {"reference_seconds": [], "native_seconds": []}
    for cls, _ in implementations:
        [cls(value) for _ in range(count)]
    for iteration in range(repeat):
        order = implementations if iteration % 2 == 0 else reversed(implementations)
        for cls, key in order:
            start = perf_counter()
            values = [cls(value) for _ in range(count)]
            samples[key].append(perf_counter() - start)
            del values
    ref = statistics.median(samples["reference_seconds"])
    native = statistics.median(samples["native_seconds"])
    return {**samples, "reference_median_seconds": ref,
            "native_median_seconds": native, "speedup_reference_over_native": ref / native}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--repeat", type=int, default=9)
    args = parser.parse_args()
    if args.count < 1 or args.repeat < 1:
        parser.error("count and repeat must be positive")
    report = {"count": args.count, "repeat": args.repeat, "python": sys.version,
              "platform": platform.platform(), "cases": {
                  name: measure(value, args.count, args.repeat)
                  for name, value in CASES.items()}}
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
