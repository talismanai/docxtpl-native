"""The select, reject, selectattr, rejectattr and map filters match Jinja."""

from __future__ import annotations

import re
import unittest
from typing import Any
from unittest.mock import patch

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import filters, pass_context, pass_environment, pass_eval_context
from jinja2.sandbox import SandboxedEnvironment


class User:
    def __init__(self, name: str, active: bool) -> None:
        self.name = name
        self.active = active


class Recording(jinja2.Environment):
    """Overrides the per-item dispatch, which the filters must call."""

    def call_filter(self, name: Any, value: Any, *args: Any, **kwargs: Any) -> Any:
        return f'filter:{name}:{value}'

    def call_test(self, name: Any, value: Any, *args: Any, **kwargs: Any) -> bool:
        return value == 2


def environment(kind: str, asynchronous: bool) -> jinja2.Environment:
    if kind == 'sandbox':
        result: jinja2.Environment = SandboxedEnvironment(enable_async=asynchronous)
    elif kind == 'recording':
        result = Recording(enable_async=asynchronous)
    else:
        result = jinja2.Environment(enable_async=asynchronous)
    result.filters['context_name'] = pass_context(lambda context, value: f'{value}:{context.name}')
    result.filters['eval_autoescape'] = pass_eval_context(lambda context, value: f'{value}:{context.autoescape}')
    result.filters['environment_type'] = pass_environment(lambda env, value: f'{value}:{type(env).__name__}')
    result.tests['context_a'] = pass_context(lambda context, value: value == 'a')
    return result


def outcome(native: bool, source: str, kind: str = 'plain', asynchronous: bool = False) -> tuple[Any, ...]:
    env = environment(kind, asynchronous)
    values = {'numbers': [1, 2, 3, 4, 0], 'users': [User('a', True), User('b', False)], 'empty': [],
              'records': [{'x': {'y': 1}}, {'x': {'y': 2}}], 'words': ['a', 'B'], 'number': 5,
              'lists': [[1], [2]], 'generator': (n for n in [1, 0, 2])}
    try:
        if native:
            output = docxtpl_native._extension().PythonRenderSession(
                BridgeConfig(env, False), values
            ).render('case', source)
        else:
            output = env.from_string(source).render(values)
        return ('output', re.sub(r' at 0x[0-9a-f]+', '', str(output)))
    except Exception as error:  # noqa: BLE001 - compared between renderers
        return (type(error).__name__, str(error), error.__cause__, error.__suppress_context__)


CASES = (
    '{{ numbers|select }}', '{{ numbers|select|list }}', '{{ numbers|select("odd")|list }}',
    '{{ numbers|reject("odd")|list }}', '{{ numbers|select("divisibleby", 2)|list }}',
    '{{ numbers|select("missing")|list }}', '{{ numbers|select(missing)|list }}', '{{ empty|select("missing")|list }}',
    '{{ generator|reject|list }}', '{{ number|select|list }}', '{% set pending = numbers|select("missing") %}lazy',
    '{{ users|selectattr("active")|map(attribute="name")|list }}',
    '{{ users|rejectattr("active")|map(attribute="name")|list }}',
    '{{ users|selectattr("name", "equalto", "a")|map(attribute="name")|list }}',
    '{{ users|selectattr("name", "in", ["a"])|list|length }}', '{{ users|selectattr|list }}',
    '{{ words|select("context_a")|list }}',
    '{{ records|map(attribute="x.y")|list }}', '{{ records|map(attribute="z", default=5)|list }}',
    '{{ records|map(attribute="z", other=1)|list }}', '{{ records|map|list }}', '{{ users|map(attribute="missing")|list }}',
    '{{ words|map }}', '{{ words|map("upper")|list }}', '{{ words|map("replace", "a", "c")|list }}',
    '{{ words|map("missing")|list }}', '{{ empty|map("upper")|list }}', '{{ words|map("context_name")|list }}',
    '{{ words|map("eval_autoescape")|list }}', '{{ words|map("environment_type")|list }}',
    '{{ lists|map("first")|list }}', '{{ numbers|map("first")|list }}', '{{ words|map("sum")|list }}',
    '{{ numbers|map("string")|join }}',
)


class SelectMapFilterTests(unittest.TestCase):
    def test_outputs_and_errors_match_reference(self) -> None:
        for kind in ('plain', 'sandbox', 'recording'):
            for asynchronous in (False, True):
                for source in CASES:
                    with self.subTest(kind=kind, asynchronous=asynchronous, source=source):
                        self.assertEqual(outcome(True, source, kind, asynchronous),
                                         outcome(False, source, kind, asynchronous))

    def test_replaced_helpers_are_used(self) -> None:
        def prepare_map(context: Any, args: Any, kwargs: Any) -> Any:
            return lambda item: f'mapped:{item}'

        def prepare_select_or_reject(*args: Any) -> Any:
            return lambda item: item == 3

        for name, replacement in (('prepare_map', prepare_map), ('prepare_select_or_reject', prepare_select_or_reject)):
            with patch.object(filters, name, replacement):
                for source in ('{{ numbers|map("upper")|list }}', '{{ numbers|select("odd")|list }}'):
                    with self.subTest(name=name, source=source):
                        self.assertEqual(outcome(True, source), outcome(False, source))


if __name__ == '__main__':
    unittest.main()
