"""Differential checks for recovery after an included template fails."""

import asyncio
import inspect
import unittest
from typing import cast
from unittest.mock import patch

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import runtime


class IncludeCleanupTests(unittest.TestCase):
    def test_recovered_include_failure_preserves_outer_context(self) -> None:
        def observe(native: bool, suspended: bool, with_context: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            failure = ValueError('included body failed')
            depth = 0

            def fail_once() -> str:
                events.append('call')
                if events.count('call') == 1:
                    raise failure
                return 'ok'

            async def suspended_call() -> str:
                await asyncio.sleep(0)
                return fail_once()

            async def auto_await(value: object) -> object:
                nonlocal depth
                if inspect.isawaitable(value):
                    depth += 1
                    try:
                        return await value
                    except ValueError as error:
                        if error is not failure or depth != 1:
                            raise
                        events.append('caught')
                        return 'recovered'
                    finally:
                        depth -= 1
                return value

            environment = jinja2.Environment(enable_async=True, loader=jinja2.DictLoader({
                'part': '{% with x="inner" %}{% set value %}{{fallible()}}{% endset %}{{value}}{% endwith %}',
            }))
            cast(dict[str, object], environment.globals)['fallible'] = suspended_call if suspended else fail_once
            mode = '' if with_context else ' without context'
            source = ('{% if false %}{% block outer %}pre{% include "part"' + mode +
                      ' %}post{% endblock %}{% endif %}[{{self.outer()}}|{{root}}|{{self.outer()}}]')
            with patch.object(runtime, 'auto_await', auto_await):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'root': 'outside'}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(root='outside')
            return output, events

        for suspended in (False, True):
            for with_context in (False, True):
                with self.subTest(suspended=suspended, with_context=with_context):
                    expected = observe(False, suspended, with_context)
                    self.assertEqual(expected, ('[recovered|outside|preokpost]', ['call', 'caught', 'call']))
                    self.assertEqual(observe(True, suspended, with_context), expected)
