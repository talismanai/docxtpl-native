"""Differential binding and sandbox checks for recursive loop calls."""

import sys
import unittest
import warnings
from typing import Any

import docxtpl
import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2.runtime import AsyncLoopContext, Context, LoopContext
from jinja2.sandbox import SandboxedEnvironment, SecurityError
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template


class RecursiveLoopArgumentTests(unittest.TestCase):
    def observe(self, native: bool, asynchronous: bool, sandboxed: bool,
                recursive: bool, expression: str, context: dict[str, Any]) -> tuple[str, str]:
        environment = (SandboxedEnvironment if sandboxed else jinja2.Environment)(enable_async=asynchronous)
        marker = ' recursive' if recursive else ''
        source = '{% for x in [1]' + marker + ' %}{{ ' + expression + ' }}{% endfor %}'
        try:
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context
                ).render('case', source)
            else:
                output = environment.from_string(source).render(context)
            return 'ok', output
        except Exception as error:  # noqa: BLE001 - compare exception type and message
            return type(error).__name__, str(error)

    def test_binding_and_nonrecursive_errors(self) -> None:
        cases: list[tuple[str, dict[str, Any]]] = [
            ('loop()', {}),
            ('loop([])', {}),
            ('loop([], [])', {}),
            ('loop(iterable=[])', {}),
            ('loop(foo=[])', {}),
            ('loop(*args)', {'args': []}),
            ('loop(*args)', {'args': [[]]}),
            ('loop(**kw)', {'kw': {}}),
            ('loop(**kw)', {'kw': {'iterable': []}}),
            ('loop(**kw)', {'kw': {1: []}}),
            ('loop(iterable=[], **kw)', {'kw': {'iterable': []}}),
            ('loop(1)', {}),
        ]
        for asynchronous in (False, True):
            for sandboxed in (False, True):
                for recursive in (False, True):
                    for expression, context in cases:
                        with self.subTest(asynchronous=asynchronous, sandboxed=sandboxed,
                                          recursive=recursive, expression=expression, context=context):
                            self.assertEqual(
                                self.observe(True, asynchronous, sandboxed, recursive, expression, context),
                                self.observe(False, asynchronous, sandboxed, recursive, expression, context),
                            )

    def test_recursive_keywords_preserve_output_and_complete_document(self) -> None:
        source = ('{% for node in nodes recursive %}{{ node.name }}'
                  '{% if node.children %}[{{ loop(iterable=node.children) }}]{% endif %}{% endfor %}')
        values = {'nodes': [{'name': 'A', 'children': [{'name': 'B', 'children': []}]}]}
        original = template(source)
        reference = generate(docxtpl, source, values, source=original)
        candidate = generate(docxtpl_native, source, values, source=original)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])
        forbidden = {LoopContext.__call__.__code__, LoopContext.__init__.__code__, LoopContext.__next__.__code__,
                     LoopContext._to_iterator.__code__, AsyncLoopContext.__anext__.__code__,
                     AsyncLoopContext._to_iterator.__code__}
        previous = sys.getprofile()

        def profile(frame: Any, event: str, argument: Any) -> None:
            if event == 'call' and frame.f_code in forbidden:
                raise AssertionError('reference loop algorithm executed')

        try:
            sys.setprofile(profile)
            for asynchronous in (False, True):
                environment = jinja2.Environment(enable_async=asynchronous)
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
                self.assertEqual(output, 'A[B]')
        finally:
            sys.setprofile(previous)

    def test_custom_sandbox_callable_policy_is_not_bypassed(self) -> None:
        class Environment(SandboxedEnvironment):
            def is_safe_callable(self, obj: Any) -> bool:
                return False

        for asynchronous in (False, True):
            environment = Environment(enable_async=asynchronous)
            source = '{% for x in [1] recursive %}{{ loop([]) }}{% endfor %}'
            with warnings.catch_warnings():
                warnings.filterwarnings('ignore', message="coroutine 'AsyncLoopContext.length' was never awaited")
                with self.assertRaises(SecurityError):
                    environment.from_string(source).render()
            session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            # The live loop object reaches the custom policy, which refuses it.
            with self.subTest(asynchronous=asynchronous), warnings.catch_warnings():
                warnings.filterwarnings('ignore', message="coroutine 'AsyncLoopContext.length' was never awaited")
                with self.assertRaisesRegex(SecurityError, 'is not safely callable'):
                    session.render('case', source)

    def test_sandbox_loop_class_unsafe_marker_is_refused(self) -> None:
        missing = object()
        previous = LoopContext.__dict__.get('unsafe_callable', missing)
        setattr(LoopContext, 'unsafe_callable', True)  # noqa: B010 - temporary policy marker
        try:
            for asynchronous in (False, True):
                with self.subTest(asynchronous=asynchronous):
                    environment = SandboxedEnvironment(enable_async=asynchronous)
                    source = '{% for x in [1] recursive %}{{ loop([]) }}{% endfor %}'
                    with warnings.catch_warnings():
                        warnings.filterwarnings('ignore', message="coroutine 'AsyncLoopContext.length' was never awaited")
                        with self.assertRaises(SecurityError):
                            environment.from_string(source).render()
                    session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                    # The marked class reaches the sandbox policy on the live loop.
                    with warnings.catch_warnings():
                        warnings.filterwarnings('ignore', message="coroutine 'AsyncLoopContext.length' was never awaited")
                        with self.assertRaisesRegex(SecurityError, 'is not safely callable'):
                            session.render('case', source)
        finally:
            if previous is missing:
                delattr(LoopContext, 'unsafe_callable')
            else:
                setattr(LoopContext, 'unsafe_callable', previous)  # noqa: B010 - restore policy marker

    def test_custom_context_call_receives_loop(self) -> None:
        def observe(native: bool, asynchronous: bool, sandboxed: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class CustomContext(Context):
                def call(self, callback: Any, *args: Any, **kwargs: Any) -> Any:
                    events.append('context.call')
                    return 'handled'

            environment = (SandboxedEnvironment if sandboxed else jinja2.Environment)(enable_async=asynchronous)
            environment.context_class = CustomContext
            source = '{% for x in [1] recursive %}{{ loop([]) }}{% endfor %}'
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render()
                result = 'ok:' + output
            except Exception as error:  # noqa: BLE001 - preserve unsupported callback gap
                result = type(error).__name__
            return result, events

        for asynchronous in (False, True):
            for sandboxed in (False, True):
                with self.subTest(asynchronous=asynchronous, sandboxed=sandboxed):
                    self.assertEqual(observe(True, asynchronous, sandboxed),
                                     observe(False, asynchronous, sandboxed))

    def test_alias_recursion_uses_defining_loop_depth(self) -> None:
        source = (
            '{% for node in nodes recursive %}'
            '{{ loop.depth }}:{{ node.name }}'
            '{% if node.children %}{% set recurse = loop %}'
            '{% for inner in [1] %}({{ loop.depth }}[{{ recurse(iterable=node.children) }}]){% endfor %}'
            '{% endif %}{% endfor %}'
        )
        values = {'nodes': [{'name': 'A', 'children': [
            {'name': 'B', 'children': [{'name': 'C', 'children': []}]}
        ]}]}
        for asynchronous in (False, True):
            for sandboxed in (False, True):
                with self.subTest(asynchronous=asynchronous, sandboxed=sandboxed):
                    environment = (SandboxedEnvironment if sandboxed else jinja2.Environment)(enable_async=asynchronous)
                    reference = environment.from_string(source).render(values)
                    self.assertEqual(reference, '1:A(1[2:B(1[3:C])])')
                    candidate = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), values
                    ).render('case', source)
                    self.assertEqual(candidate, reference)

    def test_binding_errors_preserve_handled_exception_context(self) -> None:
        def observe(native: bool, asynchronous: bool, expression: str) -> tuple[str, str, bool]:
            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% for x in [1] %}{{ ' + expression + ' }}{% endfor %}'
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
            raise AssertionError('loop call did not fail')

        for asynchronous in (False, True):
            for expression in ('loop()', 'loop([])', 'loop(foo=[])'):
                with self.subTest(asynchronous=asynchronous, expression=expression):
                    self.assertEqual(observe(True, asynchronous, expression),
                                     observe(False, asynchronous, expression))


if __name__ == '__main__':
    unittest.main()
