"""Classifying a Python value runs none of its class's Python code, as in Jinja."""

from __future__ import annotations

import unittest
from collections.abc import Iterator
from typing import Any

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig


def observe(native: bool) -> tuple[str, list[str]]:
    missing: list[str] = []

    class Meta(type):
        def __getattr__(cls, name: str) -> Any:
            missing.append(name)
            raise AttributeError(name)

    class Record(metaclass=Meta):
        def __init__(self, index: int) -> None:
            self.label = f'row{index}'

        def __getitem__(self, key: str) -> str:
            return self.label

    class Rows(metaclass=Meta):
        def __iter__(self) -> Iterator[Record]:
            return iter([Record(1), Record(2)])

    source = ('{% for item in rows %}{{ item.label }}={{ item["x"] }};{% endfor %}'
              '{{ rows|list|length }}{{ record is iterable }}{{ rows is iterable }}')
    values = {'rows': Rows(), 'record': Record(0)}
    environment = jinja2.Environment()
    if native:
        output = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), values).render(
            'case', source)
    else:
        output = environment.from_string(source).render(values)
    return str(output), missing


class ObjectKindTests(unittest.TestCase):
    def test_metaclass_lookups_match_reference(self) -> None:
        self.assertEqual(observe(True), observe(False))


if __name__ == '__main__':
    unittest.main()
