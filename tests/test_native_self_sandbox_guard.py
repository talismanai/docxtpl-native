"""Sandboxed ``self.<block>`` calls go through a real BlockReference, as in Jinja.

These scenarios used to pin a provisional refusal (UnsupportedFeature) until
self blocks had a sandbox adapter. Each now requires the reference renderer's
output or exception and the same observable events.
"""

import re
import unittest
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import docxtpl_native
from docxtpl_native.bridge import BridgeConfig
from jinja2 import DictLoader, Environment, runtime
from jinja2.sandbox import SandboxedEnvironment


def _render(native: bool, environment: Environment, source: str, values: dict[str, Any]) -> tuple[str, str]:
    """The output, or the exception type and message, of one render."""
    try:
        if native:
            output = docxtpl_native._extension().PythonRenderSession(
                BridgeConfig(environment, False), values
            ).render('case', source)
        else:
            output = environment.from_string(source).render(values)
        return ('output', str(output))
    except Exception as error:  # noqa: BLE001 - compared between renderers
        # Messages can name objects by address.
        return (type(error).__name__, re.sub(r' at 0x[0-9a-f]+', '', str(error)))


class SelfSandboxGuardTests(unittest.TestCase):
    def assert_matches_reference(self, observe: Callable[[bool], Any]) -> Any:
        expected = observe(False)
        self.assertEqual(observe(True), expected)
        return expected

    def test_mapping_error_precedes_sandbox_guard(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool, asynchronous: bool = asynchronous) -> tuple[Any, bool]:
                    original = ValueError('mapping failure')

                    class Mapping:
                        def keys(self) -> list[str]:
                            raise original

                        def __getitem__(self, key: str) -> int:
                            raise AssertionError(key)

                    environment = SandboxedEnvironment(enable_async=asynchronous)
                    source = ('{% if false %}{% block body %}BODY{% endblock %}{% endif %}'
                              '{{self.body(**mapping)}}')
                    try:
                        _render_raising(native, environment, source, {'mapping': Mapping()})
                    except ValueError as error:
                        return ('ValueError', str(error)), error is original
                    raise AssertionError('expected the mapping error')

                self.assertEqual(self.assert_matches_reference(observe), (('ValueError', 'mapping failure'), True))

    def test_mapping_is_expanded_before_the_call(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool, asynchronous: bool = asynchronous) -> tuple[tuple[str, str], list[str]]:
                    events: list[str] = []

                    class Mapping:
                        def keys(self) -> list[str]:
                            events.append('keys')
                            return ['key']

                        def __getitem__(self, key: str) -> int:
                            events.append('get:' + key)
                            return 7

                    environment = SandboxedEnvironment(enable_async=asynchronous)
                    source = ('{% if false %}{% block body %}BODY{% endblock %}{% endif %}'
                              '{{self.body(**mapping)}}')
                    return _render(native, environment, source, {'mapping': Mapping()}), events

                outcome, events = self.assert_matches_reference(observe)
                self.assertEqual(outcome[0], 'TypeError')
                self.assertEqual(events, ['keys', 'get:key'])

    def test_argument_runs_before_the_call(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool, asynchronous: bool = asynchronous) -> tuple[tuple[str, str], list[str]]:
                    events: list[str] = []

                    def argument() -> int:
                        events.append('argument')
                        return 7

                    environment = SandboxedEnvironment(enable_async=asynchronous)
                    source = ('{% if false %}{% block body %}BODY{% endblock %}{% endif %}'
                              '{{self.body(argument())}}')
                    return _render(native, environment, source, {'argument': argument}), events

                outcome, events = self.assert_matches_reference(observe)
                self.assertEqual(outcome[0], 'TypeError')
                self.assertEqual(events, ['argument'])

    def test_custom_auto_await_receives_the_block_call(self) -> None:
        def observe(native: bool) -> tuple[tuple[str, str], list[str]]:
            events: list[str] = []

            async def auto_await(value: object) -> object:
                events.append('auto_await')
                return await value if hasattr(value, '__await__') else value  # ty: ignore[invalid-await]

            def body() -> str:
                events.append('body')
                return 'BODY'

            source = '{% if false %}{% block body %}{{body()}}{% endblock %}{% endif %}{{self.body()}}'
            environment = SandboxedEnvironment(enable_async=True)
            with patch.object(runtime, 'auto_await', auto_await):
                return _render(native, environment, source, {'body': body}), events

        self.assertEqual(self.assert_matches_reference(observe)[0], ('output', 'BODY'))

    def test_sandboxed_self_calls_in_included_and_inherited_templates(self) -> None:
        templates = {
            'included': '{% if false %}{% block body %}{{body()}}{% endblock %}{% endif %}{{self.body()}}',
            'base': '{% if false %}{% block body %}{{body()}}{% endblock %}{% endif %}{{self.body()}}',
        }
        for asynchronous in (False, True):
            for source in ('{% include "included" %}', '{% extends "base" %}'):
                with self.subTest(asynchronous=asynchronous, source=source):
                    def observe(native: bool, asynchronous: bool = asynchronous,
                                source: str = source) -> tuple[tuple[str, str], list[str]]:
                        events: list[str] = []

                        def body() -> str:
                            events.append('body')
                            return 'BODY'

                        environment = SandboxedEnvironment(enable_async=asynchronous, loader=DictLoader(templates))
                        return _render(native, environment, source, {'body': body}), events

                    self.assertEqual(self.assert_matches_reference(observe), (('output', 'BODY'), ['body']))

    def test_sandbox_policy_decides_self_calls(self) -> None:
        for asynchronous in (False, True):
            for expression in ('{{self.body()}}', '{% set x = self.body() %}{{x}}',
                               '{% macro go() %}{{self.body()}}{% endmacro %}{{go()}}',
                               '{{(self.body)()}}'):
                for custom in (False, True):
                    with self.subTest(asynchronous=asynchronous, expression=expression, custom=custom):
                        def observe(native: bool, asynchronous: bool = asynchronous, expression: str = expression,
                                    custom: bool = custom) -> tuple[tuple[str, str], list[str]]:
                            events: list[str] = []

                            class Deny(SandboxedEnvironment):
                                def is_safe_callable(self, obj: Any) -> bool:
                                    events.append('policy')
                                    return type(obj).__name__ != 'BlockReference'

                            def body() -> str:
                                events.append('body')
                                return 'BODY'

                            environment = (Deny(enable_async=asynchronous) if custom
                                           else SandboxedEnvironment(enable_async=asynchronous))
                            source = ('{% if false %}{% block body %}{{body()}}{% endblock %}'
                                      '{% endif %}' + expression)
                            return _render(native, environment, source, {'body': body}), events

                        outcome, events = self.assert_matches_reference(observe)
                        if custom:
                            self.assertEqual(outcome[0], 'SecurityError')
                            self.assertNotIn('body', events)
                        else:
                            self.assertEqual(outcome, ('output', 'BODY'))

    def test_non_sandbox_self_calls_keep_their_existing_behavior(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                source = '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{self.body()}}'
                environment = Environment(enable_async=asynchronous)
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                self.assertEqual(session.render('case', source), 'BODY')


def _render_raising(native: bool, environment: Environment, source: str, values: dict[str, Any]) -> str:
    if native:
        return docxtpl_native._extension().PythonRenderSession(
            BridgeConfig(environment, False), values
        ).render('case', source)
    return environment.from_string(source).render(values)


if __name__ == '__main__':
    unittest.main()
