"""Check lifetimes of dispatchers after a caught nested block error."""

import asyncio
import inspect
import unittest
import weakref
from typing import Any
from unittest.mock import patch

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import runtime


class CallUnwindTests(unittest.TestCase):
    def test_caught_block_error_releases_pending_dispatcher(self) -> None:
        def observe(native: bool, suspending: bool) -> tuple[str, list[bool]]:
            dispatchers: list[weakref.ReferenceType[Any]] = []
            checks: list[bool] = []

            class Dispatcher:
                def __init__(self, original: Any) -> None:
                    self.original = original

                def __call__(self, *args: Any, **kwargs: Any) -> Any:
                    return self.original(*args, **kwargs)

            class Context(runtime.Context):
                def __getattribute__(self, name: str) -> Any:
                    result = super().__getattribute__(name)
                    if name != 'call':
                        return result
                    dispatcher = Dispatcher(result)
                    dispatchers.append(weakref.ref(dispatcher))
                    return dispatcher

            def fallible() -> int:
                raise ValueError('failed')

            async def fallible_async() -> int:
                await asyncio.sleep(0)
                raise ValueError('failed')

            def consume(value: int) -> int:
                return value

            def probe() -> str:
                checks.append(dispatchers[1]() is None)
                return 'checked'

            async def auto_await(value: object) -> object:
                if inspect.isawaitable(value):
                    try:
                        return await value
                    except ValueError:
                        if getattr(value, '__name__', None) == 'fallible_async':
                            raise
                        return 'recovered'
                return value

            environment = jinja2.Environment(enable_async=True)
            environment.context_class = Context
            source = ('{% if false %}{% block body %}{{consume(fallible())}}{% endblock %}{% endif %}'
                      '{{self.body()}}|{{probe()}}')
            values = {'fallible': fallible_async if suspending else fallible, 'consume': consume, 'probe': probe}
            with patch.object(runtime, 'auto_await', auto_await):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), values
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values)
            return output, checks

        for suspending in (False, True):
            with self.subTest(suspending=suspending):
                expected = observe(False, suspending)
                self.assertEqual(expected, ('recovered|checked', [True]))
                self.assertEqual(observe(True, suspending), expected)
