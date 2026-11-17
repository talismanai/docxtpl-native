"""In async renders the async_variant filters match Jinja's async bodies."""

from __future__ import annotations

import re
import unittest
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import patch

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import async_utils, filters


class Item:
    def __init__(self, size: int, name: str) -> None:
        self.size = size
        self.name = name


class Sized:
    """list() asks for a length hint; Jinja's async bodies only iterate."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __iter__(self) -> Iterator[int]:
        self.events.append('iter')
        return iter([3, 1, 2])

    def __len__(self) -> int:
        self.events.append('len')
        return 3


class Stream:
    def __aiter__(self) -> AsyncIterator[int]:
        return self.generate()

    async def generate(self) -> AsyncIterator[int]:
        for value in (2, 1, 2):
            yield value


def outcome(native: bool, source: str, values: dict[str, Any]) -> tuple[str, str]:
    environment = jinja2.Environment(enable_async=True)
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


def values(events: list[str]) -> dict[str, Any]:
    return {'numbers': [3, 1, 2], 'empty': [], 'text': 'abca', 'number': 4, 'sized': Sized(events),
            'stream': Stream(), 'lists': [[1], [2]], 'mixed': [1, 'a'],
            'items': [Item(2, 'b'), Item(5, 'A'), Item(2, 'a')], 'generator': (n for n in [5, 6, 5])}


CASES = (
    '{{ numbers|first }}', '{{ empty|first is defined }}', '{{ number|first }}', '{{ generator|first }}',
    '{{ stream|first }}', '{{ sized|first }}',
    '{{ numbers|list }}', '{{ text|list }}', '{{ sized|list }}', '{{ stream|list }}', '{{ number|list }}',
    '{{ numbers|sum }}', '{{ numbers|sum(start=10) }}', '{{ items|sum(attribute="size") }}', '{{ lists|sum(start=[]) }}',
    '{{ mixed|sum }}', '{{ stream|sum }}', '{{ sized|sum }}',
    '{{ items|groupby("size")|map(attribute="grouper")|list }}', '{{ items|groupby("name")|list|length }}',
    '{{ items|groupby("name", case_sensitive=true)|map(attribute="grouper")|list }}', '{{ stream|groupby(0)|list }}',
    '{{ numbers|slice(2) }}', '{{ numbers|slice(2)|list }}', '{{ sized|slice(2, 0)|list }}', '{{ stream|slice(2)|list }}',
    '{{ text|unique }}', '{{ text|unique|list }}', '{{ items|unique(attribute="name")|map(attribute="size")|list }}',
    '{{ stream|unique|list }}', '{{ number|unique|list }}',
    '{{ numbers|select }}', '{{ numbers|select("odd")|list }}', '{{ numbers|reject("odd")|list }}',
    '{{ stream|select("even")|list }}', '{{ sized|select|list }}', '{{ empty|select("missing")|list }}',
    '{{ numbers|select("missing")|list }}', '{{ items|selectattr("size", "equalto", 2)|map(attribute="name")|list }}',
    '{{ items|rejectattr("size", "equalto", 2)|map(attribute="name")|list }}', '{{ items|selectattr|list }}',
    '{{ number|select|list }}', '{{ numbers|map }}', '{{ numbers|map("string")|list }}', '{{ stream|map("string")|list }}',
    '{{ items|map(attribute="name")|map("lower")|list }}', '{{ lists|map("first")|list }}', '{{ lists|map("sum")|list }}',
    '{{ items|map(attribute="missing", default=0)|list }}', '{{ items|map|list }}', '{{ items|map(attribute="a", x=1)|list }}',
    '{{ numbers|map("missing")|list }}', '{{ number|map("string")|list }}',
)


class AsyncVariantFilterTests(unittest.TestCase):
    def test_outputs_errors_and_protocol_calls_match_reference(self) -> None:
        for source in CASES:
            with self.subTest(source=source):
                native_events: list[str] = []
                reference_events: list[str] = []
                self.assertEqual(outcome(True, source, values(native_events)),
                                 outcome(False, source, values(reference_events)))
                self.assertEqual(native_events, reference_events)

    def test_sum_adds_in_place_as_the_async_body(self) -> None:
        for native in (True, False):
            start: list[int] = []
            outcome(native, '{{ lists|sum(start=start) }}', {**values([]), 'start': start})
            self.assertEqual(start, [1, 2], native)

    def test_replaced_async_helper_is_used(self) -> None:
        calls: list[str] = []
        original = async_utils.auto_to_list

        async def auto_to_list(value: Any) -> list[Any]:
            calls.append('auto_to_list')
            return await original(value)

        with patch.object(filters, 'auto_to_list', auto_to_list):
            for native in (True, False):
                self.assertEqual(outcome(native, '{{ numbers|list }}', values([])), ('output', '[3, 1, 2]'))
        self.assertEqual(calls, ['auto_to_list', 'auto_to_list'])


if __name__ == '__main__':
    unittest.main()
