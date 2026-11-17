"""The ``string`` and ``escape`` filters run MarkupSafe's algorithms in Rust."""

from __future__ import annotations

import unittest
from typing import Any

import docxtpl_native
import jinja2
import markupsafe
from docxtpl_native.bridge import BridgeConfig


class Html:
    def __html__(self) -> str:
        return '<b>safe</b>'

    def __str__(self) -> str:
        return '<b>unsafe</b>'


class Text:
    def __str__(self) -> str:
        return 'a<b'


def render(native: bool, source: str, values: dict[str, Any], autoescape: bool = False) -> str:
    environment = jinja2.Environment(autoescape=autoescape)
    if native:
        return docxtpl_native._extension().PythonRenderSession(
            BridgeConfig(environment, False), values
        ).render('case', source)
    return environment.from_string(source).render(values)


class MarkupSafeFilterTests(unittest.TestCase):
    values: dict[str, Any] = {
        'plain': 'a<b & "c"', 'markup': markupsafe.Markup('<i>x</i>'), 'number': 7,
        'html': Html(), 'text': Text(), 'none': None,
    }

    def test_outputs_match_reference(self) -> None:
        for name in self.values:
            for expression in ('{{ %s|escape }}', '{{ %s|e }}', '{{ %s|string }}', '{{ (%s|string)|escape }}',
                               '{{ %s|escape|length }}', '{{ (%s|string) is string }}'):
                for autoescape in (False, True):
                    source = expression % name
                    with self.subTest(source=source, autoescape=autoescape):
                        self.assertEqual(render(True, source, self.values, autoescape),
                                         render(False, source, self.values, autoescape))

    def test_replaced_filters_remain_callbacks(self) -> None:
        events: list[str] = []

        def custom(value: object) -> str:
            events.append('custom')
            return 'custom'

        for name in ('escape', 'string'):
            with self.subTest(name=name):
                events.clear()
                environment = jinja2.Environment()
                environment.filters[name] = custom
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'value': 'x'}
                ).render('case', '{{ value|' + name + ' }}')
                self.assertEqual((output, events), ('custom', ['custom']))


if __name__ == '__main__':
    unittest.main()
