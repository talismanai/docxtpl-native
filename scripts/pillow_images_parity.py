"""Differential test: picture handling of docxtpl-pycompat vs Pillow.

Generates pictures with Pillow in every format and mode the crate reads
(PNG, JPEG, GIF, BMP, WebP, TIFF; palette, grayscale, alpha, CMYK, LAB,
float, 16-bit, compressed TIFF, animated GIF/WebP, resolution tags) and
compares, per picture, what ``Image.open`` reports (format, size,
``info["dpi"]``), whether ``image.save(buffer, "PNG")`` succeeds, and the
decoded pixels (first frame, RGBA). Lossy formats (JPEG, lossy WebP) are
compared within a tolerance (32 levels), since decoders round differently.

Usage: python scripts/pillow_images_parity.py INSPECT_BINARY [CASES] [SEED]
"""

from __future__ import annotations

import io
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

LOSSY = {"JPEG", "WEBP_LOSSY"}


def picture(rng: random.Random) -> tuple[bytes, str]:
    width, height = rng.randint(1, 120), rng.randint(1, 120)
    kind = rng.choice(["PNG", "JPEG", "GIF", "BMP", "WEBP", "WEBP_LOSSY", "TIFF"])
    modes = {
        "PNG": ["RGB", "RGBA", "L", "LA", "P", "1", "I;16"],
        "JPEG": ["RGB", "L", "CMYK"],
        "GIF": ["P", "L", "RGB"],
        "BMP": ["RGB", "RGBA", "L", "P", "1"],
        "WEBP": ["RGB", "RGBA"],
        "WEBP_LOSSY": ["RGB", "RGBA"],
        "TIFF": ["RGB", "RGBA", "L", "P", "1", "CMYK", "LAB", "F", "I;16", "LA"],
    }[kind]
    mode = rng.choice(modes)
    noise = Image.effect_noise((width, height), rng.uniform(5, 80)).convert("L")
    base = Image.merge("RGB", [noise, noise.rotate(90, expand=False), noise.transpose(0)])
    image = base.convert(mode) if mode not in {"F", "I;16", "LAB"} else (
        noise.convert("F") if mode == "F" else noise.convert("I;16") if mode == "I;16" else base.convert("LAB")
    )
    buffer = io.BytesIO()
    options: dict[str, Any] = {}
    if rng.random() < 0.5 and kind in {"PNG", "JPEG", "BMP", "TIFF"}:
        options["dpi"] = (rng.choice([72, 96, 150, 300, 600]),) * 2
    if kind == "TIFF":
        options["compression"] = rng.choice([None, "tiff_lzw", "tiff_adobe_deflate", "packbits"])
        if options["compression"] is None:
            del options["compression"]
        if rng.random() < 0.3:
            options["resolution_unit"] = rng.choice([1, 2, 3])
            options["dpi"] = (rng.choice([72, 118, 300]),) * 2
    if kind in {"GIF", "WEBP"} and rng.random() < 0.3:
        frames = [image, image.rotate(90)]
        frames[0].save(buffer, "GIF" if kind == "GIF" else "WEBP", save_all=True, append_images=frames[1:], **({"lossless": True} if kind == "WEBP" else {}))
        return buffer.getvalue(), kind
    if kind == "WEBP":
        image.save(buffer, "WEBP", lossless=True)
    elif kind == "WEBP_LOSSY":
        image.save(buffer, "WEBP", quality=80)
    else:
        image.save(buffer, kind, **options)
    return buffer.getvalue(), kind


def embedded_pixels(image: Image.Image) -> bytes:
    """RGBA pixels of the PNG Pillow writes, 16-bit samples as their high byte."""
    if image.mode in {"I;16", "I;16L", "I;16B"}:
        raw = image.tobytes()
        high = raw[0::2] if image.mode == "I;16B" else raw[1::2]
        return Image.frombytes("L", image.size, high).convert("RGBA").tobytes()
    return image.convert("RGBA").tobytes()


def reference(data: bytes) -> dict[str, Any] | None:
    try:
        image = Image.open(io.BytesIO(data))
    except Exception:  # noqa: BLE001 - an unreadable picture is a compared outcome
        return None
    dpi = image.info.get("dpi")
    try:
        image.save(io.BytesIO(), "PNG")
        writable = True
    except Exception:  # noqa: BLE001 - Pillow refusing the mode is the compared outcome
        writable = False
    return {
        "format": image.format,
        "size": list(image.size),
        "dpi": [float(dpi[0]), float(dpi[1])] if dpi else None,
        "png_writable": writable,
        "pixels": embedded_pixels(image) if writable else None,
    }


def main() -> int:
    binary, cases = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 500
    rng = random.Random(int(sys.argv[3]) if len(sys.argv) > 3 else 1)
    failures = 0
    with tempfile.TemporaryDirectory() as folder:
        for case in range(cases):
            data, kind = picture(rng)
            path, decoded = Path(folder, "in"), Path(folder, "out.png")
            path.write_bytes(data)
            decoded.unlink(missing_ok=True)
            run = subprocess.run([binary, str(path), str(decoded)], capture_output=True, text=True, check=True)
            actual = json.loads(run.stdout)
            expected = reference(data)
            problems = []
            if expected is None or actual is None:
                if (expected is None) != (actual is None):
                    problems.append(f"open: pillow={expected is not None} rust={actual is not None}")
            else:
                if actual["format"].upper() != expected["format"]:
                    problems.append(f"format {actual['format']} vs {expected['format']}")
                if actual["size"] != expected["size"]:
                    problems.append(f"size {actual['size']} vs {expected['size']}")
                if (actual["dpi"] is None) != (expected["dpi"] is None) or (
                    actual["dpi"] and any(abs(a - b) > 1e-6 for a, b in zip(actual["dpi"], expected["dpi"]))
                ):
                    problems.append(f"dpi {actual['dpi']} vs {expected['dpi']}")
                if actual["png_writable"] != expected["png_writable"]:
                    problems.append(f"png_writable {actual['png_writable']} vs {expected['png_writable']}")
                if expected["pixels"] is not None:
                    if not decoded.exists():
                        problems.append("not decoded")
                    else:
                        mine = Image.open(decoded).convert("RGBA").tobytes()
                        theirs = expected["pixels"]
                        if len(mine) != len(theirs):
                            problems.append("pixel buffer size")
                        elif kind in LOSSY:
                            worst = max(abs(a - b) for a, b in zip(mine, theirs))
                            if worst > 32:
                                problems.append(f"lossy pixels differ by up to {worst}")
                        elif mine != theirs:
                            problems.append("pixels differ")
            if problems:
                failures += 1
                print(f"case {case} ({kind}): {'; '.join(problems)}")
    print(f"{cases - failures}/{cases} equal to Pillow {Image.__version__}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
