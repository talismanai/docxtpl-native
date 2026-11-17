"""Differential checks for globals used by required-block declarations."""

import builtins
import unittest
from typing import Any
from unittest.mock import patch

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import runtime


class RequiredRuntimeGlobalsTests(unittest.TestCase):
    def test_synchronous_runtime_import_hook_order(self) -> None:
        def observe(native: bool) -> list[tuple[str, int, tuple[str, ...]]]:
            events: list[tuple[str, int, tuple[str, ...]]] = []
            original = builtins.__import__
            environment = jinja2.Environment()
            configuration = BridgeConfig(environment, False)
            if native:
                docxtpl_native._extension()

            def importing(name: str, globals: Any = None, locals: Any = None,
                          fromlist: Any = (), level: int = 0) -> Any:
                if name in ('runtime', 'jinja2.runtime'):
                    events.append((name, level, tuple(fromlist or ())))
                return original(name, globals, locals, fromlist, level)

            with patch.object(builtins, '__import__', importing):
                if native:
                    docxtpl_native._extension().PythonRenderSession(configuration, {}).render('case', 'literal')
                else:
                    environment.from_string('literal').render()
            return events

        expected = observe(False)
        self.assertEqual(len(expected), 3)
        self.assertNotIn('auto_await', expected[-1][2])
        self.assertEqual(observe(True), expected)

    def test_missing_runtime_export_raises_name_error(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, str, Any]:
            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% block body required %}{% endblock %}'
            exports = [name for name in runtime.exported if name != 'TemplateRuntimeError']
            with patch.object(runtime, 'exported', exports):
                try:
                    if native:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {}
                        ).render('case', source)
                    else:
                        environment.from_string(source).render()
                except Exception as error:  # noqa: BLE001 - compare observable error
                    return type(error).__name__, str(error), getattr(error, 'name', None)
            raise AssertionError('expected missing runtime global')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = ('NameError', "name 'TemplateRuntimeError' is not defined", 'TemplateRuntimeError')
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_error_constructor_exception_keeps_identity(self) -> None:
        for native in (False, True):
            for asynchronous in (False, True):
                with self.subTest(native=native, asynchronous=asynchronous):
                    marker = ValueError('constructor failed')

                    def constructor(message: str, failure: ValueError = marker) -> None:
                        raise failure

                    environment = jinja2.Environment(enable_async=asynchronous)
                    source = '{% block body required %}{% endblock %}'
                    with patch.object(runtime, 'TemplateRuntimeError', constructor), self.assertRaises(ValueError) as caught:
                        if native:
                            docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {}
                            ).render('case', source)
                        else:
                            environment.from_string(source).render()
                    self.assertIs(caught.exception, marker)

    def test_error_constructor_returning_non_exception(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, str]:
            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% block body required %}{% endblock %}'
            with patch.object(runtime, 'TemplateRuntimeError', lambda message: None):
                try:
                    if native:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {}
                        ).render('case', source)
                    else:
                        environment.from_string(source).render()
                except Exception as error:  # noqa: BLE001 - compare observable error
                    return type(error).__name__, str(error)
            raise AssertionError('expected invalid exception error')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = ('TypeError', 'exceptions must derive from BaseException')
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_error_constructor_is_captured_before_context_initialization(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, str, list[str]]:
            events: list[str] = []
            original = runtime.TemplateRuntimeError

            class CapturedError(RuntimeError):
                def __init__(self, message: str) -> None:
                    events.append('captured constructor')
                    super().__init__(message)

            class Context(runtime.Context):
                def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                             globals: Any = None) -> None:
                    events.append('context')
                    runtime.TemplateRuntimeError = original
                    super().__init__(environment, parent, name, blocks, globals=globals)

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% block body required %}{% endblock %}'
            with patch.object(runtime, 'TemplateRuntimeError', CapturedError):
                try:
                    if native:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {}
                        ).render('case', source)
                    else:
                        environment.from_string(source).render()
                except Exception as error:  # noqa: BLE001 - compare exception type and message
                    return type(error).__name__, str(error), events
            raise AssertionError('expected required-block error')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = ('CapturedError', "Required block 'body' not found", ['context', 'captured constructor'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)
