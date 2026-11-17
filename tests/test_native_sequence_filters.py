"""The first, list, sum and groupby filters match Jinja in sync and async renders."""

from __future__ import annotations

import unittest
from typing import Any

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig


class Item:
    def __init__(self, size: int) -> None:
        self.size = size


def outcome(native: bool, source: str, values: dict[str, Any], asynchronous: bool) -> tuple[str, str]:
    environment = jinja2.Environment(enable_async=asynchronous)
    try:
        if native:
            output = docxtpl_native._extension().PythonRenderSession(
                BridgeConfig(environment, False), values
            ).render('case', source)
        else:
            output = environment.from_string(source).render(values)
        return ('output', str(output))
    except Exception as error:  # noqa: BLE001 - compared between renderers
        return (type(error).__name__, str(error))


def values() -> dict[str, Any]:
    return {'numbers': [3, 1, 2], 'empty': [], 'text': 'abc', 'items': [Item(2), Item(5)],
            'records': [{'n': {'v': 2}}, {'n': {'v': 3}}], 'mixed': [1, 'a'], 'number': 4,
            'generator': (n for n in [5, 6]),
            'people': [{'city': 'B', 'name': 'x'}, {'city': 'a', 'name': 'y'}, {'city': 'A', 'name': 'z'},
                       {'name': 'w'}]}


CASES = (
    '{{ numbers|first }}', '{{ empty|first is defined }}', '{{ text|first }}', '{{ generator|first }}',
    '{{ number|first }}', '{{ numbers|list }}', '{{ text|list }}', '{{ generator|list }}', '{{ number|list }}',
    '{{ numbers|sum }}', '{{ numbers|sum(start=10) }}', '{{ items|sum(attribute="size") }}',
    '{{ records|sum(attribute="n.v") }}', '{{ mixed|sum }}', '{{ items|sum(attribute="missing") }}',
    '{{ number|sum }}',
    '{{ people|groupby("city", default="none") }}', '{{ people|groupby("city", default="none", case_sensitive=true) }}',
    '{% for city, items in people|groupby("city", default="-") %}{{ city }}:{{ items|length }};{% endfor %}',
    '{{ people|groupby("city") }}', '{{ (people|groupby("name"))[0].grouper }}', '{{ numbers|groupby(0) }}',
)


class SequenceFilterTests(unittest.TestCase):
    def test_outputs_and_errors_match_reference(self) -> None:
        for source in CASES:
            for asynchronous in (False, True):
                with self.subTest(source=source, asynchronous=asynchronous):
                    self.assertEqual(outcome(True, source, values(), asynchronous),
                                     outcome(False, source, values(), asynchronous))


if __name__ == '__main__':
    unittest.main()
