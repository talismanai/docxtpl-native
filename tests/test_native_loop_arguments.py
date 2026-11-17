"""Differential argument binding for native loop methods."""

import unittest
from typing import Any

import docxtpl
import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2.sandbox import SandboxedEnvironment
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template


class LoopArgumentTests(unittest.TestCase):
    def observe(self, native: bool, asynchronous: bool, sandboxed: bool,
                expression: str, values: dict[str, Any]) -> tuple[str, str]:
        environment = (SandboxedEnvironment if sandboxed else jinja2.Environment)(enable_async=asynchronous)
        source = '{% for item in [1] %}{{ ' + expression + ' }}{% endfor %}'
        try:
            if native:
                result = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                result = environment.from_string(source).render(values)
            return 'ok', result
        except Exception as error:  # noqa: BLE001 - compare actual error types and messages
            return type(error).__name__, str(error)

    def test_loop_method_arguments(self) -> None:
        cases: list[tuple[str, dict[str, Any]]] = [
            ('loop.cycle()', {}),
            ('loop.cycle(1)', {}),
            ('loop.cycle(1, **kw)', {'kw': {}}),
            ('loop.cycle(**kw)', {'kw': {}}),
            ('loop.changed(**kw)', {'kw': {}}),
            ('loop.changed(1, **kw)', {'kw': {}}),
            ('loop.cycle(a=1)', {}),
            ('loop.changed(a=1)', {}),
            ('loop.cycle(**kw)', {'kw': {'key': 1}}),
            ('loop.changed(**kw)', {'kw': {1: 1}}),
            ('loop.changed(**kw)', {'kw': None}),
            ('loop.changed(a=1, **kw)', {'kw': {'a': 2}}),
            ('loop.cycle(a=1, **kw)', {'kw': {'a': 2}}),
            ('loop.changed(**kw)', {'kw': {'_loop_vars': 2}}),
            ('loop.changed(_block_vars=1)', {}),
            ("loop['cycle']()", {}),
            ("loop['cycle'](1, **kw)", {'kw': {}}),
            ("loop['changed'](**kw)", {'kw': {'key': 1}}),
            ("loop['changed'](**kw)", {'kw': {1: 1}}),
            ("loop['changed'](a=1, **kw)", {'kw': {'a': 2}}),
        ]
        for asynchronous in (False, True):
            for sandboxed in (False, True):
                for expression, values in cases:
                    with self.subTest(asynchronous=asynchronous, sandboxed=sandboxed, expression=expression, values=values):
                        self.assertEqual(
                            self.observe(True, asynchronous, sandboxed, expression, values),
                            self.observe(False, asynchronous, sandboxed, expression, values),
                        )

    def test_empty_keywords_preserve_changed_state_in_complete_document(self) -> None:
        source = '{% for item in items %}{{ loop.changed(**kw) }};{% endfor %}'
        original = template(source)
        context = {'items': [1, 2], 'kw': {}}
        reference = generate(docxtpl, source, context, source=original)
        candidate = generate(docxtpl_native, source, context, source=original)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_keyword_mapping_protocol_and_exception_identity(self) -> None:
        def observe(native: bool, asynchronous: bool, fail: bool) -> tuple[Any, list[str]]:
            events: list[str] = []
            failure = RuntimeError('mapping failed')

            class Mapping:
                def keys(self) -> list[str]:
                    events.append('keys')
                    return ['key']

                def __getitem__(self, key: str) -> int:
                    events.append('item:' + key)
                    if fail:
                        raise failure
                    return 1

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% for item in [1] %}{{ loop.changed(**kw) }}{% endfor %}'
            try:
                if native:
                    text = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'kw': Mapping()}
                    ).render('case', source)
                else:
                    text = environment.from_string(source).render(kw=Mapping())
                result: Any = ('ok', text)
            except Exception as error:  # noqa: BLE001 - compare exact error and original identity
                result = (type(error).__name__, str(error), error is failure)
            return result, events

        for asynchronous in (False, True):
            for fail in (False, True):
                with self.subTest(asynchronous=asynchronous, fail=fail):
                    self.assertEqual(observe(True, asynchronous, fail), observe(False, asynchronous, fail))

    def test_cycle_error_preserves_handled_exception_context(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, str, bool]:
            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% for item in [1] %}{{ loop.cycle() }}{% endfor %}'
            try:
                raise ValueError('outer')
            except ValueError as outer:
                try:
                    if native:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {}
                        ).render('case', source)
                    else:
                        environment.from_string(source).render()
                except Exception as error:  # noqa: BLE001 - compare exception chaining
                    return type(error).__name__, str(error), error.__context__ is outer
            raise AssertionError('loop.cycle() did not fail')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))


if __name__ == '__main__':
    unittest.main()
