"""The items, batch, slice and unique filters match Jinja's generators."""

from __future__ import annotations

import re
import unittest
from collections.abc import Iterator, Mapping
from typing import Any
from unittest.mock import patch

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import filters


class Pairs(Mapping[str, int]):
    def __getitem__(self, key: str) -> int:
        return {'a': 1, 'b': 2}[key]

    def __iter__(self) -> Iterator[str]:
        return iter(['a', 'b'])

    def __len__(self) -> int:
        return 2


def outcome(native: bool, source: str, values: dict[str, Any], asynchronous: bool) -> tuple[str, str]:
    environment = jinja2.Environment(enable_async=asynchronous)
    try:
        if native:
            output = docxtpl_native._extension().PythonRenderSession(
                BridgeConfig(environment, False), values
            ).render('case', source)
        else:
            output = environment.from_string(source).render(values)
        return ('output', re.sub(r' at 0x[0-9a-f]+', '', str(output)))
    except Exception as error:  # noqa: BLE001 - compared between renderers
        return (type(error).__name__, str(error))


def values() -> dict[str, Any]:
    return {'mapping': {'a': 1, 'b': 2}, 'pairs': Pairs(), 'numbers': list(range(7)), 'six': range(6),
            'empty': [], 'number': 5, 'text': 'abcde', 'words': ['a', 'A', 'b', 'a'],
            'records': [{'n': {'m': 'x'}}, {'n': {'m': 'X'}}, {'n': {'m': 'y'}}], 'lists': [[1], [1]],
            'generator': (n for n in [1, 2, 1, 3])}


CASES = (
    '{{ mapping|items }}', '{{ mapping|items|list }}', '{{ pairs|items|list }}', '{{ missing|items|list }}',
    '{{ numbers|items|list }}', '{% set pending = numbers|items %}lazy',
    '{{ numbers|batch(3) }}', '{{ numbers|batch(3)|list }}', '{{ numbers|batch(3, "-")|list }}',
    '{{ six|batch(3, "-")|list }}', '{{ empty|batch(3)|list }}', '{{ numbers|batch(0)|list }}',
    '{{ number|batch(2)|list }}', '{% for row in text|batch(2) %}{{ row|join }};{% endfor %}',
    '{{ numbers|slice(3) }}', '{{ numbers|slice(3)|list }}', '{{ numbers|slice(3, 0)|list }}',
    '{{ six|slice(4, "-")|list }}', '{{ numbers|slice(0)|list }}', '{{ number|slice(2)|list }}',
    '{% set pending = numbers|slice(0) %}lazy',
    '{{ words|unique }}', '{{ words|unique|list }}', '{{ words|unique(true)|list }}',
    '{{ records|unique(attribute="n.m")|list }}', '{{ records|unique(true, "n.m")|list }}',
    '{{ lists|unique|list }}', '{{ number|unique|list }}', '{{ generator|unique|list }}',
)


class GeneratorFilterTests(unittest.TestCase):
    def test_outputs_and_errors_match_reference(self) -> None:
        for source in CASES:
            for asynchronous in (False, True):
                with self.subTest(source=source, asynchronous=asynchronous):
                    self.assertEqual(outcome(True, source, values(), asynchronous),
                                     outcome(False, source, values(), asynchronous))

    def test_replaced_helper_is_used(self) -> None:
        def upper(value: Any) -> Any:
            return value.upper() if isinstance(value, str) else value

        with patch.object(filters, 'ignore_case', upper):
            for source in ('{{ words|unique|list }}', '{{ records|unique(attribute="n.m")|list }}'):
                with self.subTest(source=source):
                    self.assertEqual(outcome(True, source, values(), False), outcome(False, source, values(), False))


if __name__ == '__main__':
    unittest.main()
