"""Differential fuzz: the native patch_xml vs docxtpl's regular expressions.

Random strings of WordprocessingML markup, Jinja delimiters and docxtpl's
structural tags go through ``DocxTemplate.patch_xml`` of both engines, which
must return the same string or raise the same exception type. ``{%-`` and
``-%}`` are left out: the native merge does not cross a table, row or cell
boundary, a deliberate difference
(docs/STATUS.md#deliberate-differences-from-docxtpl).

Usage: PYTHONPATH=python .venv/bin/python scripts/patch_xml_fuzz.py [CASES] [SEED]
"""
from __future__ import annotations

import random
import sys
import warnings
from typing import Any

PIECES = ['<w:p>', '</w:p>', '<w:p w:x="1">', '<w:p/>', '<w:tr>', '</w:tr>', '<w:tc>', '</w:tc>', '<w:tcPr>', '</w:tcPr>',
          '<w:r>', '</w:r>', '<w:t>', '</w:t>', '<w:t xml:space="preserve">', '</w:t></w:r><w:r><w:t>', '<w:tbl>',
          '</w:tbl>', 'x', ' ', '\n', '{', '}', '%', '#', '{{', '}}', '{%', '%}', '{#', '#}', '&lt;', '&gt;', '“', '’',
          '{%p if a %}', '{%p endif %}', '{{p v }}', '{#p c #}', '{%tr for x in y %}', '{%tr endfor %}',
          '{%tc if b %}', '{%tc endif %}', '{%r if c %}', '{%r endif %}', '{{r rt }}', '{{ v }}', '{% if a %}',
          '{% endif %}', '{# c #}', 'colspan', 'cellbg', '{% colspan 2 %}', '{% cellbg "FF0000" %}', '{% vm %}',
          '{% hm %}']


def outcome(engine: Any, source: str) -> str:
    try:
        return engine.patch_xml(source)
    except Exception as error:  # noqa: BLE001 - the exception type is compared
        return 'error: ' + type(error).__name__


def main(cases: int, seed: int) -> int:
    warnings.simplefilter('ignore')
    import docxtpl
    import docxtpl_native
    reference = docxtpl.DocxTemplate(None)  # ty: ignore[invalid-argument-type]
    native = docxtpl_native.DocxTemplate(None)
    generator = random.Random(seed)
    failures = 0
    for _ in range(cases):
        source = ''.join(generator.choice(PIECES) for _ in range(generator.randint(1, 40)))
        expected, actual = outcome(reference, source), outcome(native, source)
        if expected != actual:
            failures += 1
            if failures <= 5:
                print(repr(source), '\n  docxtpl:', repr(expected), '\n  native: ', repr(actual))
    print(f'{cases} cases, {failures} differ')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 20000,
                          int(sys.argv[2]) if len(sys.argv) > 2 else 0))
