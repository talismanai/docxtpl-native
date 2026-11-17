"""The urlencode, wordwrap, pprint, random and attr filters match Jinja."""

from __future__ import annotations

import random
import unittest
from typing import Any

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2.sandbox import SandboxedEnvironment


class Thing:
    visible = 'visible'

    def __init__(self) -> None:
        self.value = 1

    def __getattr__(self, name: str) -> str:
        if name == 'dynamic':
            return 'dynamic'
        raise AttributeError(name)


def outcome(native: bool, source: str, values: dict[str, Any], sandboxed: bool = False) -> tuple[str, str]:
    environment = (SandboxedEnvironment if sandboxed else jinja2.Environment)()
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
    'text': 'a b/c&d é', 'bytes': b'x y', 'number': 7, 'mapping': {'a b': 'c&d', 1: None},
    'pairs': [('k', 'v'), ('x y', 1)], 'bad_pairs': [('a', 'b', 'c')], 'flat': [1], 'short': [('a',)],
    'long': 'one two three four five six\nseven eight-nine ten', 'nested': {'b': [1, {'c': 2}], 'a': (1,)},
    'empty': [], 'single': ['only'], 'thing': Thing(), 'data': {'key': 'item'},
}

CASES = (
    '{{ text|urlencode }}', '{{ bytes|urlencode }}', '{{ number|urlencode }}', '{{ mapping|urlencode }}',
    '{{ pairs|urlencode }}', '{{ bad_pairs|urlencode }}', '{{ flat|urlencode }}', '{{ short|urlencode }}',
    '{{ long|wordwrap(10) }}', '{{ long|wordwrap(5, false) }}', '{{ long|wordwrap(12, wrapstring="|") }}',
    '{{ long|wordwrap(8, break_on_hyphens=false) }}', '{{ nested|pprint }}', '{{ text|pprint }}',
    '{{ single|random }}', '{{ empty|random is defined }}', '{{ thing|attr("value") }}', '{{ thing|attr("visible") }}',
    '{{ thing|attr("dynamic") }}', '{{ thing|attr("missing") is defined }}', '{{ data|attr("key") is defined }}',
    '{{ thing|attr("__class__") }}',
)


class TextFilterTests(unittest.TestCase):
    def test_outputs_and_errors_match_reference(self) -> None:
        for source in CASES:
            for sandboxed in (False, True):
                with self.subTest(source=source, sandboxed=sandboxed):
                    self.assertEqual(outcome(True, source, dict(VALUES), sandboxed),
                                     outcome(False, source, dict(VALUES), sandboxed))

    def test_random_uses_the_module_random(self) -> None:
        source = '{{ [1, 2, 3, 4, 5]|random }}{{ [1, 2, 3, 4, 5]|random }}'
        random.seed(5)
        reference = outcome(False, source, {})
        random.seed(5)
        self.assertEqual(outcome(True, source, {}), reference)


if __name__ == '__main__':
    unittest.main()
