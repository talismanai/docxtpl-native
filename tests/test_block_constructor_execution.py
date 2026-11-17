"""Block callables are usable during context construction."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

import jinja2
from jinja2 import runtime

import docxtpl_native
from docxtpl_native.bridge import BridgeConfig


class BlockConstructorExecutionTests(unittest.TestCase):
    def test_retained_block_can_execute_after_render(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, str]:
            retained: list[Any] = []

            @jinja2.pass_context
            def retain(context: Any) -> str:
                retained.append(context)
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{{ retain() }}{% block body %}Hello {{ name }}{% endblock %}'
            values = {'retain': retain, 'name': 'Ada'}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            context = retained[0]
            generator: Any = context.blocks['body'][0](context)

            async def collect() -> str:
                return ''.join([fragment async for fragment in generator])

            block_output = asyncio.run(collect()) if asynchronous else ''.join(generator)
            return output, block_output

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('Hello Ada', 'Hello Ada'))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_context_constructor_can_run_block_before_root(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(runtime.Context):
                def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                             globals: Any = None) -> None:
                    super().__init__(environment, parent, name, blocks, globals=globals)
                    events.append('before')
                    generator: Any = self.blocks['body'][0](self)
                    if asynchronous:
                        try:
                            getattr(generator, '__anext__')().send(None)
                        except StopIteration as result:
                            events.append(result.value)
                        else:
                            raise AssertionError('static block unexpectedly suspended')
                        try:
                            getattr(generator, 'aclose')().send(None)
                        except StopIteration:
                            pass
                        else:
                            raise AssertionError('static block close unexpectedly suspended')
                    else:
                        events.append(''.join(generator))
                    events.append('after')

            def on_root() -> str:
                events.append('root')
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{{ on_root() }}{% block body %}BLOCK{% endblock %}'
            if native:
                result = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'on_root': on_root}
                ).render('case', source)
            else:
                result = environment.from_string(source).render(on_root=on_root)
            return result, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('BLOCK', ['before', 'BLOCK', 'after', 'root']))
                self.assertEqual(observe(True, asynchronous), expected)


if __name__ == '__main__':
    unittest.main()
