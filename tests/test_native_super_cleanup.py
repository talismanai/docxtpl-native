"""Differential checks for a recovered failure inside a parent block."""

import asyncio
import inspect
import sys
import unittest
from collections.abc import Awaitable, Iterator
from types import FrameType
from typing import cast
from unittest.mock import patch

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import runtime
from jinja2.nodes import EvalContext
from jinja2.sandbox import SandboxedEnvironment, SecurityError


class SuperCleanupTests(unittest.TestCase):
    def test_self_block_allowed_by_sandbox_can_render(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                source = '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{self.body()}}'
                reference_env = SandboxedEnvironment(enable_async=asynchronous)
                native_env = SandboxedEnvironment(enable_async=asynchronous)
                reference = reference_env.from_string(source).render()
                self.assertEqual(reference, 'BODY')
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(native_env, False), {}
                ).render('case', source)
                self.assertEqual(output, reference)

    def test_self_block_does_not_execute_standard_python_call_body(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            calls: list[str] = []
            codes = {runtime.BlockReference.__call__.__code__, runtime.BlockReference._async_call.__code__}
            previous = sys.getprofile()

            def profile(frame: FrameType, event: str, argument: object) -> None:
                if event == 'call' and frame.f_code in codes:
                    calls.append(frame.f_code.co_name)

            environment = jinja2.Environment(enable_async=True)
            source = '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{self.body()}}'
            try:
                sys.setprofile(profile)
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {}).render('case', source)
                else:
                    output = environment.from_string(source).render()
            finally:
                sys.setprofile(previous)
            return output, calls

        reference, reference_calls = observe(False)
        self.assertEqual(reference, 'BODY')
        self.assertIn('_async_call', reference_calls)
        self.assertEqual(observe(True), ('BODY', []))

    def test_self_block_auto_await_result_obeys_autoescape(self) -> None:
        def observe(native: bool, autoescape: bool) -> str:
            async def auto_await(value: object) -> object:
                if inspect.iscoroutine(value):
                    value.close()
                    return '<replacement>'
                return value

            environment = jinja2.Environment(enable_async=True, autoescape=autoescape)
            source = ('{% if false %}{% block body %}BODY{% endblock %}{% endif %}'
                      '[{{self.body()}}]')
            with patch.object(runtime, 'auto_await', auto_await):
                if native:
                    return docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                return environment.from_string(source).render()

        for autoescape in (False, True):
            with self.subTest(autoescape=autoescape):
                expected = observe(False, autoescape)
                self.assertEqual(expected, '[&lt;replacement&gt;]' if autoescape else '[<replacement>]')
                self.assertEqual(observe(True, autoescape), expected)

    def test_nested_self_block_calls_keep_await_and_scope_order(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def emit() -> str:
                events.append('body')
                return 'B'

            async def auto_await(value: object) -> object:
                if inspect.iscoroutine(value):
                    events.append('before')
                    await asyncio.sleep(0)
                    result = await value
                    events.append('after')
                    return result
                return value

            environment = jinja2.Environment(enable_async=True)
            source = ('{% if false %}{% block outer %}A{{self.inner()}}C{% endblock %}'
                      '{% block inner %}{{emit()}}{% endblock %}{% endif %}{{self.outer()}}')
            with patch.object(runtime, 'auto_await', auto_await):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'emit': emit}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(emit=emit)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('ABC', ['before', 'before', 'body', 'after', 'after']))
        self.assertEqual(observe(True), expected)

    def test_self_block_argument_evaluated_before_type_error(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def argument() -> int:
                events.append('argument')
                return 1

            environment = jinja2.Environment(enable_async=asynchronous)
            source = ('{% if false %}{% block body %}BODY{% endblock %}{% endif %}'
                      '{{self.body(argument())}}')
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'argument': argument}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(argument=argument)
            except TypeError as error:
                output = str(error)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('BlockReference.__call__() takes 1 positional argument but 2 were given', ['argument']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_self_block_argument_forms_follow_reference(self) -> None:
        def observe(native: bool, asynchronous: bool, expression: str) -> tuple[str, list[str]]:
            events: list[str] = []

            def argument() -> int:
                events.append('argument')
                return 7

            def body() -> str:
                events.append('body')
                return 'BODY'

            env = jinja2.Environment(enable_async=asynchronous)
            source = ('{% if false %}{% block body %}{{body()}}{% endblock %}{% endif %}'
                      '{{self.body(' + expression + ')}}')
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(env, False), {'argument': argument, 'body': body}
                    ).render('case', source)
                else:
                    output = env.from_string(source).render(argument=argument, body=body)
            except TypeError as error:
                output = type(error).__name__ + ':' + str(error)
            return output, events

        for asynchronous in (False, True):
            for expression in ('argument()', 'key=argument()', '*[]', '*[argument()]', '**{}', '**{"key": argument()}'):
                with self.subTest(asynchronous=asynchronous, expression=expression):
                    self.assertEqual(observe(True, asynchronous, expression), observe(False, asynchronous, expression))

    def test_self_block_argument_protocols_and_exceptions(self) -> None:
        def observe(native: bool, asynchronous: bool, expression: str) -> tuple[str, list[str], bool]:
            events: list[str] = []
            failure = ValueError('argument failed')

            class Sequence:
                def __iter__(self) -> Iterator[int]:
                    events.append('iter')
                    raise failure

            class Mapping:
                def keys(self) -> list[str]:
                    events.append('keys')
                    return ['key']

                def __getitem__(self, key: str) -> int:
                    events.append('get:' + key)
                    return 4

            def argument() -> int:
                events.append('argument')
                return 7

            environment = jinja2.Environment(enable_async=asynchronous)
            source = ('{% if false %}{% block body %}BODY{% endblock %}{% endif %}'
                      '{{self.body(' + expression + ')}}')
            values = {'argument': argument, 'items': Sequence(), 'mapping': Mapping()}
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), values).render('case', source)
                else:
                    output = environment.from_string(source).render(values)
            except Exception as error:  # noqa: BLE001 - compare type, message, identity and effects
                return type(error).__name__ + ':' + str(error), events, error is failure
            return output, events, False

        for asynchronous in (False, True):
            for expression in ('*items', '**mapping', 'key=argument(), **mapping', 'argument(), key=argument()'):
                with self.subTest(asynchronous=asynchronous, expression=expression):
                    self.assertEqual(observe(True, asynchronous, expression), observe(False, asynchronous, expression))

    def test_self_block_expansions_inside_loop_follow_reference(self) -> None:
        def observe(native: bool, asynchronous: bool, expression: str) -> tuple[str, list[str]]:
            events: list[str] = []

            def argument() -> int:
                events.append('argument')
                return 7

            environment = jinja2.Environment(enable_async=asynchronous)
            source = ('{% if false %}{% block body %}BODY{% endblock %}{% endif %}'
                      '{% for item in [1] %}{{self.body(' + expression + ')}}{% endfor %}')
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'argument': argument}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(argument=argument)
            except (TypeError, SyntaxError) as error:
                output = type(error).__name__ + ':' + str(error)
            return output, events

        for asynchronous in (False, True):
            for expression in ('**{}', '**{"key": argument()}', '_loop_vars=1'):
                with self.subTest(asynchronous=asynchronous, expression=expression):
                    self.assertEqual(observe(True, asynchronous, expression), observe(False, asynchronous, expression))

    def test_custom_self_block_call_override_remains_observable(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def custom(self: object, value: int) -> str:
                events.append('custom')
                return f'custom:{value}'

            environment = jinja2.Environment()
            source = '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{self.body(7)}}'
            with patch.object(runtime.BlockReference, '__call__', custom):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {}).render('case', source)
                else:
                    output = environment.from_string(source).render()
            return output, events

        self.assertEqual(observe(False), ('custom:7', ['custom']))
        self.assertEqual(observe(True), ('custom:7', ['custom']))

    def test_custom_self_block_no_argument_override_remains_observable(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def custom(self: object) -> str:
                events.append('custom')
                return 'custom'

            environment = jinja2.Environment()
            source = '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{self.body()}}'
            with patch.object(runtime.BlockReference, '__call__', custom):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {}).render('case', source)
                else:
                    output = environment.from_string(source).render()
            return output, events

        self.assertEqual(observe(False), ('custom', ['custom']))
        self.assertEqual(observe(True), ('custom', ['custom']))

    def test_custom_context_call_intercepts_self_block_arguments(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(runtime.Context):
                def call(self, obj: object, *args: object, **kwargs: object) -> str:
                    events.append('call:' + type(obj).__name__)
                    return 'intercepted'

            environment = jinja2.Environment()
            environment.context_class = Context
            source = '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{self.body(7)}}'
            if native:
                output = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {}).render('case', source)
            else:
                output = environment.from_string(source).render()
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('intercepted', ['call:BlockReference']))
        self.assertEqual(observe(True), expected)

    def test_self_block_obeys_sandbox_callable_policy(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[tuple[str, str]]]:
            events: list[tuple[str, str]] = []

            class Environment(SandboxedEnvironment):
                def is_safe_callable(self, obj: object) -> bool:
                    events.append((type(obj).__name__, str(getattr(obj, 'name', ''))))
                    return False

            environment = Environment(enable_async=asynchronous)
            source = '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{self.body()}}'
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render()
            except SecurityError:
                output = 'security error'
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('security error', [('BlockReference', 'body')]))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_self_block_coroutine_can_be_awaited_after_render(self) -> None:
        def observe(native: bool) -> tuple[str, str, list[str]]:
            retained: list[object] = []
            events: list[str] = []

            def emit() -> str:
                events.append('body')
                return 'body'

            async def auto_await(value: object) -> object:
                if inspect.iscoroutine(value):
                    retained.append(value)
                    return 'held'
                return value

            environment = jinja2.Environment(enable_async=True)
            source = ('{% if false %}{% block body %}{{emit()}}{% endblock %}{% endif %}'
                      '[{{self.body()}}]')
            with patch.object(runtime, 'auto_await', auto_await):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'emit': emit}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(emit=emit)
            self.assertEqual(len(retained), 1)

            async def resume() -> str:
                result = await cast(Awaitable[object], retained[0])
                return str(result)

            return output, asyncio.run(resume()), events

        expected = observe(False)
        self.assertEqual(expected, ('[held]', 'body', ['body']))
        self.assertEqual(observe(True), expected)

    def test_self_block_auto_await_controls_execution(self) -> None:
        def observe(native: bool, mode: str) -> tuple[str, list[str]]:
            events: list[str] = []

            def emit() -> str:
                events.append('body')
                return 'body'

            async def auto_await(value: object) -> object:
                if inspect.iscoroutine(value):
                    events.append('before')
                    await asyncio.sleep(0)
                    if mode == 'close':
                        value.close()
                        events.append('closed')
                        return 'skipped'
                    result = await value
                    events.append('after')
                    return result
                return value

            environment = jinja2.Environment(enable_async=True)
            source = ('{% if false %}{% block body %}{{emit()}}{% endblock %}{% endif %}'
                      '[{{self.body()}}]')
            with patch.object(runtime, 'auto_await', auto_await):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'emit': emit}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(emit=emit)
            return output, events

        for mode in ('await', 'close'):
            with self.subTest(mode=mode):
                expected = observe(False, mode)
                self.assertEqual(expected, ('[body]', ['before', 'body', 'after']) if mode == 'await'
                                 else ('[skipped]', ['before', 'closed']))
                self.assertEqual(observe(True, mode), expected)

    def test_caught_self_block_failure_preserves_outer_scope(self) -> None:
        def observe(native: bool, outer: bool) -> tuple[str, list[bool]]:
            flags: list[bool] = []

            def fail() -> str:
                raise ValueError('block failed')

            @jinja2.pass_eval_context
            def flag(eval_context: EvalContext, value: str) -> str:
                flags.append(eval_context.autoescape)
                return value

            async def auto_await(value: object) -> object:
                if inspect.isawaitable(value):
                    try:
                        return await value
                    except ValueError:
                        return 'recovered'
                return value

            inner = 'false' if outer else 'true'
            env = jinja2.Environment(enable_async=True, autoescape=outer)
            env.filters['flag'] = flag
            source = ('{% if false %}{% block body %}{% with x="inner" %}{% autoescape ' + inner +
                      ' %}{% set value %}{{fail()}}{% endset %}{% endautoescape %}{% endwith %}'
                      '{% endblock %}{% endif %}[{{self.body()}}|{{tag|flag}}]')
            values = {'fail': fail, 'tag': '<tag>'}
            with patch.object(runtime, 'auto_await', auto_await):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(BridgeConfig(env, False), values).render('case', source)
                else:
                    output = env.from_string(source).render(values)
            return output, flags

        for outer in (False, True):
            with self.subTest(outer=outer):
                expected = observe(False, outer)
                self.assertEqual(expected, ('[recovered|&lt;tag&gt;]' if outer else '[recovered|<tag>]', [not outer]))
                self.assertEqual(observe(True, outer), expected)

    def test_caught_parent_failure_preserves_lexical_escape_and_eval_context_effects(self) -> None:
        def observe(native: bool, outer: bool) -> tuple[str, list[bool]]:
            flags: list[bool] = []

            def fail() -> str:
                raise ValueError('parent failed')

            @jinja2.pass_eval_context
            def flag(eval_context: EvalContext, value: str) -> str:
                flags.append(eval_context.autoescape)
                return value

            async def auto_await(value: object) -> object:
                if inspect.isawaitable(value):
                    try:
                        return await value
                    except ValueError:
                        return 'recovered'
                return value

            inner = 'false' if outer else 'true'
            env = jinja2.Environment(enable_async=True, autoescape=outer, loader=jinja2.DictLoader({
                'base': ('{% block body %}{% with x="inner" %}{% autoescape ' + inner +
                         ' %}{{fail()}}{% endautoescape %}{% endwith %}{% endblock %}'),
            }))
            env.filters['flag'] = flag
            source = '{% extends "base" %}{% block body %}[{{super()}}|{{tag|flag}}]{% endblock %}'
            values = {'fail': fail, 'tag': '<tag>'}
            with patch.object(runtime, 'auto_await', auto_await):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(BridgeConfig(env, False), values).render('case', source)
                else:
                    output = env.from_string(source).render(values)
            return output, flags

        for outer in (False, True):
            with self.subTest(outer=outer):
                expected = observe(False, outer)
                self.assertEqual(expected, ('[recovered|&lt;tag&gt;]' if outer else '[recovered|<tag>]', [not outer]))
                self.assertEqual(observe(True, outer), expected)

    def test_caught_parent_failure_allows_later_super_call(self) -> None:
        def observe(native: bool, asynchronous: bool, nested: bool, autoescape: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            failure = ValueError('parent failed')
            depth = 0

            def fallible() -> str:
                events.append('parent')
                if len(events) == 1:
                    raise failure
                return 'ok'

            async def async_fallible() -> str:
                await asyncio.sleep(0)
                return fallible()

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

            expression = ('{% set value %}{{fallible()}}{% endset %}{{value}}'
                          if nested else '{{fallible()}}')
            env = jinja2.Environment(enable_async=True, autoescape=autoescape, loader=jinja2.DictLoader({
                'base': '{% block body %}P' + expression + '{% endblock %}',
            }))
            source = '{% extends "base" %}{% block body %}[{{super()}}|{{super()}}]{% endblock %}'
            callback = async_fallible if asynchronous else fallible
            with patch.object(runtime, 'auto_await', auto_await):
                if native:
                    result = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(env, False), {'fallible': callback}
                    ).render('case', source)
                else:
                    result = env.from_string(source).render(fallible=callback)
            return result, events

        for asynchronous in (False, True):
            for nested in (False, True):
                for autoescape in (False, True):
                    with self.subTest(asynchronous=asynchronous, nested=nested, autoescape=autoescape):
                        expected = observe(False, asynchronous, nested, autoescape)
                        self.assertEqual(expected, ('[recovered|Pok]', ['parent', 'caught', 'parent']))
                        self.assertEqual(observe(True, asynchronous, nested, autoescape), expected)
