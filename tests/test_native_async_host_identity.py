"""Observable identity and warnings for the synchronous async-render host."""

import asyncio
import gc
import unittest
import warnings
from typing import Any
from unittest.mock import patch

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig


def render(native: bool) -> Any:
    environment = jinja2.Environment(enable_async=True)
    if native:
        session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
        return session.render('case', 'hello')
    return environment.from_string('hello').render()


class AsyncHostIdentityTests(unittest.TestCase):
    def test_unawaited_warning_inside_running_loop(self) -> None:
        async def observe(native: bool) -> tuple[str, list[tuple[str, str]]]:
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter('always', RuntimeWarning)
                try:
                    render(native)
                except RuntimeError as error:
                    message = str(error)
                else:
                    raise AssertionError('expected RuntimeError')
                gc.collect()
                return message, [(item.category.__name__, str(item.message)) for item in recorded]

        expected = asyncio.run(observe(False))
        actual = asyncio.run(observe(True))
        self.assertEqual(actual, expected)
        self.assertEqual(len(expected[1]), 1)

    def test_replaced_run_observes_coroutine_identity(self) -> None:
        def observe(native: bool) -> tuple[Any, list[tuple[str, str, str]]]:
            events: list[tuple[str, str, str]] = []

            def replacement(coroutine: Any) -> str:
                events.append((type(coroutine).__name__, coroutine.__name__, coroutine.__qualname__))
                coroutine.close()
                return 'sentinel'

            with patch('asyncio.run', replacement):
                result = render(native)
            return result, events

        self.assertEqual(observe(True), observe(False))
