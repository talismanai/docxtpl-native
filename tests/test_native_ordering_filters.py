"""The sort, dictsort, min and max filters match Jinja, including errors."""

from __future__ import annotations

import unittest
from typing import Any

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2.sandbox import SandboxedEnvironment


class Item:
    def __init__(self, name: str, size: int) -> None:
        self.name = name
        self.size = size


def outcome(native: bool, source: str, values: dict[str, Any], sandboxed: bool = False) -> tuple[str, str]:
    environment = SandboxedEnvironment() if sandboxed else jinja2.Environment()
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


VALUES: dict[str, Any] = {
    'words': ['b', 'A', 'c', 'a'],
    'numbers': [3, 1, 2],
    'mixed': [1, 'a'],
    'empty': [],
    'mapping': {'b': 2, 'A': 3, 'c': 1},
    'items': [Item('b', 2), Item('a', 3), Item('C', 1)],
    'records': [{'n': {'v': 2}}, {'n': {'v': 1}}],
    'pairs': [[2, 'x'], [1, 'y']],
    'generator': (n for n in [3, 1, 2]),
}

CASES = (
    '{{ words|sort }}', '{{ words|sort(case_sensitive=true) }}', '{{ words|sort(reverse=true) }}',
    '{{ numbers|sort }}', '{{ mixed|sort }}', '{{ items|sort(attribute="name")|map(attribute="name")|list }}',
    '{{ items|sort(attribute="size,name")|map(attribute="name")|list }}', '{{ records|sort(attribute="n.v") }}',
    '{{ pairs|sort(attribute="0") }}', '{{ pairs|sort(attribute="missing") }}',
    '{{ mapping|dictsort }}', '{{ mapping|dictsort(true) }}', '{{ mapping|dictsort(by="value") }}',
    '{{ mapping|dictsort(reverse=true) }}', '{{ mapping|dictsort(by="other") }}', '{{ numbers|dictsort }}',
    '{{ words|min }}', '{{ words|max }}', '{{ words|min(case_sensitive=true) }}', '{{ numbers|max }}',
    '{{ empty|min }}', '{{ empty|max is defined }}', '{{ mixed|min }}', '{{ items|max(attribute="size")|attr("name") }}',
    '{{ records|min(attribute="n.v") }}', '{{ 5|min }}',
)


class OrderingFilterTests(unittest.TestCase):
    def test_outputs_and_errors_match_reference(self) -> None:
        for source in CASES:
            for sandboxed in (False, True):
                with self.subTest(source=source, sandboxed=sandboxed):
                    values = dict(VALUES, generator=(n for n in [3, 1, 2]))
                    reference = outcome(False, source, values, sandboxed)
                    values = dict(VALUES, generator=(n for n in [3, 1, 2]))
                    self.assertEqual(outcome(True, source, values, sandboxed), reference)

    def test_key_reads_the_environment_getitem_for_each_part(self) -> None:
        def observe(native: bool) -> tuple[tuple[str, str], list[str]]:
            events: list[str] = []

            class Environment(jinja2.Environment):
                def getitem(self, obj: Any, argument: Any) -> Any:
                    events.append(f'getitem:{argument}')
                    return super().getitem(obj, argument)

            environment = Environment()
            source = '{{ records|sort(attribute="n.v")|length }}{{ numbers|max }}'
            values = dict(VALUES)
            if native:
                result = ('output', docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values).render('case', source))
            else:
                result = ('output', environment.from_string(source).render(values))
            return result, events

        self.assertEqual(observe(True), observe(False))

    def test_replaced_helper_runs_the_python_filter(self) -> None:
        def observe(native: bool) -> tuple[str, str]:
            import jinja2.filters as filters

            original = filters.ignore_case
            filters.ignore_case = lambda value: value  # type: ignore[assignment]
            try:
                return outcome(native, '{{ words|sort }}{{ words|min }}', dict(VALUES))
            finally:
                filters.ignore_case = original

        self.assertEqual(observe(True), observe(False))


if __name__ == '__main__':
    unittest.main()
