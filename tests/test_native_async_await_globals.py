"""Differential checks for live globals in asynchronous await dispatch."""

import asyncio
import builtins
import inspect
import subprocess
import sys
import unittest
from collections.abc import Generator
from types import CodeType, CoroutineType, FrameType, SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import async_utils, runtime
from jinja2.nodes import EvalContext
from jinja2.sandbox import SandboxedEnvironment, SecurityError
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template


class AwaitGlobalsTests(unittest.TestCase):
    def test_missing_export_raises_before_call_arguments_execute(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def produce() -> str:
                events.append('produce')
                return 'x'

            environment = jinja2.Environment(enable_async=True)
            exports = [name for name in runtime.async_exported if name != 'auto_await']
            with patch.object(runtime, 'async_exported', exports):
                try:
                    if native:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {'produce': produce}
                        ).render('case', '{{ produce() }}')
                    else:
                        environment.from_string('{{ produce() }}').render(produce=produce)
                except Exception as error:  # noqa: BLE001 - compare side effect and error
                    return type(error).__name__, events
            raise AssertionError('expected missing compiled global')

        expected = observe(False)
        self.assertEqual(expected, ('NameError', []))
        self.assertEqual(observe(True), expected)

    def test_missing_export_precedes_nested_expression_side_effects(self) -> None:
        def observe(native: bool, source: str) -> tuple[str, list[str]]:
            events: list[str] = []

            def produce() -> Any:
                events.append('produce')
                return {'key': 'x'}

            environment = jinja2.Environment(enable_async=True)
            exports = [name for name in runtime.async_exported if name != 'auto_await']
            context = {'produce': produce, 'target': {'key': 'x'}}
            with patch.object(runtime, 'async_exported', exports):
                try:
                    if native:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), context
                        ).render('case', source)
                    else:
                        environment.from_string(source).render(context)
                except Exception as error:  # noqa: BLE001 - compare ordering and error
                    return type(error).__name__, events
            raise AssertionError('expected missing compiled global')

        for source in ('{{ produce().key }}', '{{ target[produce()] }}',
                       '{{ produce()|default("x") }}', '{{ produce() is string }}',
                       '{{ produce().get("key") }}'):
            with self.subTest(source=source):
                expected = observe(False, source)
                self.assertEqual(expected, ('NameError', []))
                self.assertEqual(observe(True, source), expected)

    def test_missing_export_does_not_fail_folded_expressions(self) -> None:
        def observe(native: bool, source: str) -> str:
            environment = jinja2.Environment(enable_async=True)
            exports = [name for name in runtime.async_exported if name != 'auto_await']
            with patch.object(runtime, 'async_exported', exports):
                if native:
                    return docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                return environment.from_string(source).render()

        for source in ('literal', '{{ "x"|upper }}', '{{ 1 + 2 }}'):
            with self.subTest(source=source):
                self.assertEqual(observe(True, source), observe(False, source))

    def test_runtime_exports_are_captured_before_constant_folding(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def change(value: str) -> str:
                events.append('fold')
                setattr(runtime, 'async_exported', [])  # noqa: B010 - mutate during compilation
                return value

            environment = jinja2.Environment(enable_async=True)
            environment.filters['change'] = change
            source = "{{ 'folded'|change }}{{ produce() }}"
            context = {'produce': lambda: 'x'}
            with patch.object(runtime, 'async_exported', list(runtime.async_exported)):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(context)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('foldedx', ['fold']))
        self.assertEqual(observe(True), expected)

    def test_runtime_export_list_mutation_controls_imports(self) -> None:
        def observe(native: bool, missing_export: bool) -> str:
            environment = jinja2.Environment(enable_async=True)
            exports = list(runtime.async_exported)
            exports = exports + ['missing_export_for_test'] if missing_export else [
                name for name in exports if name != 'auto_await'
            ]
            original = runtime.__dict__['auto_await']
            with patch.object(runtime, 'async_exported', exports):
                try:
                    if not missing_export:
                        del runtime.__dict__['auto_await']
                    if native:
                        return docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {}
                        ).render('case', 'literal')
                    return environment.from_string('literal').render()
                except Exception as error:  # noqa: BLE001 - compare observable compilation outcome
                    return type(error).__name__
                finally:
                    runtime.auto_await = original

        for missing_export in (False, True):
            with self.subTest(missing_export=missing_export):
                self.assertEqual(observe(True, missing_export), observe(False, missing_export))

    def test_cloned_standard_await_code_is_rejected_without_python_fallback(self) -> None:
        environment = jinja2.Environment(enable_async=True)
        session = docxtpl_native._extension().PythonRenderSession(
            BridgeConfig(environment, False), {'produce': lambda: 'x'}
        )
        self.assertEqual(session.render('case', '{{ produce() }}'), 'x')
        original = async_utils.auto_await.__code__
        cloned = original.replace()
        calls: list[str] = []

        def profile(frame: Any, event: str, argument: Any) -> None:
            if event == 'call' and frame.f_code is cloned:
                calls.append('standard')

        previous = sys.getprofile()
        try:
            async_utils.auto_await.__code__ = cloned
            self.assertEqual(environment.from_string('{{ produce() }}').render(produce=lambda: 'x'), 'x')
            sys.setprofile(profile)
            with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'auto_await provenance'):
                session.render('case', '{{ produce() }}')
        finally:
            sys.setprofile(previous)
            async_utils.auto_await.__code__ = original
        self.assertEqual(calls, [])

    def test_cloned_inspect_code_is_rejected_without_python_fallback(self) -> None:
        environment = jinja2.Environment(enable_async=True)
        session = docxtpl_native._extension().PythonRenderSession(
            BridgeConfig(environment, False), {'produce': lambda: object()}
        )
        self.assertEqual(session.render('case', '{{ produce() is not none }}'), 'True')
        original = inspect.isawaitable.__code__
        cloned = original.replace()
        calls: list[str] = []

        def profile(frame: Any, event: str, argument: Any) -> None:
            if event == 'call' and frame.f_code is cloned:
                calls.append('standard')

        previous = sys.getprofile()
        try:
            inspect.isawaitable.__code__ = cloned
            self.assertEqual(environment.from_string('{{ produce() is not none }}').render(produce=object), 'True')
            sys.setprofile(profile)
            with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'inspect.isawaitable provenance'):
                session.render('case', '{{ produce() is not none }}')
        finally:
            sys.setprofile(previous)
            inspect.isawaitable.__code__ = original
        self.assertEqual(calls, [])

    def test_runtime_import_hook_sees_generated_fromlist(self) -> None:
        def observe(native: bool) -> list[tuple[str, int, tuple[str, ...]]]:
            events: list[tuple[str, int, tuple[str, ...]]] = []
            original = builtins.__import__
            environment = jinja2.Environment(enable_async=True)
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
                    docxtpl_native._extension().PythonRenderSession(
                        configuration, {'produce': lambda: 'x'}
                    ).render('case', '{{ produce() }}')
                else:
                    environment.from_string('{{ produce() }}').render(produce=lambda: 'x')
            return events

        expected = observe(False)
        self.assertEqual(len(expected), 3)
        self.assertIn('auto_await', expected[-1][2])
        self.assertEqual(observe(True), expected)

    def test_compiler_import_hook_can_supply_export_lists(self) -> None:
        def observe(native: bool) -> tuple[str, list[tuple[str, ...]]]:
            events: list[tuple[str, ...]] = []
            original = builtins.__import__
            supplied = SimpleNamespace(exported=list(runtime.exported), async_exported=[])
            environment = jinja2.Environment(enable_async=True)
            configuration = BridgeConfig(environment, False)
            if native:
                docxtpl_native._extension()

            def importing(name: str, globals: Any = None, locals: Any = None,
                          fromlist: Any = (), level: int = 0) -> Any:
                if name == 'runtime' and level == 1:
                    events.append(tuple(fromlist or ()))
                    return supplied
                return original(name, globals, locals, fromlist, level)

            with patch.object(builtins, '__import__', importing):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        configuration, {}
                    ).render('case', 'literal')
                else:
                    output = environment.from_string('literal').render()
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('literal', [('async_exported',), ('exported',)]))
        self.assertEqual(observe(True), expected)

    def test_runtime_auto_await_callback_in_complete_document(self) -> None:
        source = template('{{ produce() }}')

        def observe(engine: Any) -> tuple[bytes, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> str:
                events.append(str(value))
                return 'replaced'

            with patch.object(runtime, 'auto_await', replacement):
                output = generate(engine, '', {'produce': lambda: 'original'},
                                  jinja2.Environment(enable_async=True), source)
            return output, events

        expected, reference = observe(docxtpl)
        actual, candidate = observe(docxtpl_native)
        self.assertEqual(reference, ['original'])
        self.assertEqual(candidate, reference)
        comparison = compare_docx(expected, actual)
        self.assertTrue(comparison['equivalent'], comparison)

    def test_preinitialization_override_and_restored_inspect(self) -> None:
        scripts = (
            '''
import inspect, sys, jinja2.async_utils, docxtpl_native
await_function = jinja2.async_utils.auto_await
inspect_function = inspect.isawaitable
before = (sys.getrefcount(await_function), sys.getrefcount(inspect_function))
docxtpl_native._extension()
after = (sys.getrefcount(await_function), sys.getrefcount(inspect_function))
assert after == before, (before, after)
''',
            '''
import jinja2, jinja2.async_utils, docxtpl_native
from docxtpl_native.bridge import BridgeConfig
events: list[str] = []
async def replacement(value: object) -> str:
    globals()['_events'].append(str(value))
    return 'custom'
original = jinja2.async_utils.auto_await.__code__
try:
    jinja2.async_utils.__dict__['_events'] = events
    jinja2.async_utils.auto_await.__code__ = replacement.__code__
    extension = docxtpl_native._extension()
    result = extension.PythonRenderSession(BridgeConfig(jinja2.Environment(enable_async=True), False),
                                            {'produce': lambda: 'x'}).render('case', '{{ produce() }}')
    assert (result, events) == ('custom', ['x']), (result, events)
finally:
    jinja2.async_utils.auto_await.__code__ = original
    del jinja2.async_utils.__dict__['_events']
environment = jinja2.Environment(enable_async=True)
result = extension.PythonRenderSession(BridgeConfig(environment, False), {}).render(
    'case', '{% for x in [1, 1] %}{{ loop.changed(x) }}{% endfor %}')
assert result == 'TrueFalse', result
''',
            '''
import inspect, sys, jinja2, jinja2.async_utils, docxtpl_native
from types import FrameType
from docxtpl_native.bridge import BridgeConfig
original = inspect.isawaitable
inspect.isawaitable = lambda value: False
extension = docxtpl_native._extension()
inspect.isawaitable = original
seen: list[str] = []
def profile(frame: FrameType, event: str, argument: object) -> None:
    if event == 'call' and frame.f_code is original.__code__:
        seen.append('standard')
previous = sys.getprofile()
try:
    sys.setprofile(profile)
    result = extension.PythonRenderSession(BridgeConfig(jinja2.Environment(enable_async=True), False),
                                            {'produce': lambda: object()}).render('case', '{{ produce() is not none }}')
finally:
    sys.setprofile(previous)
assert result == 'True' and not seen, (result, seen)
''',
            '''
import inspect, sys, jinja2, docxtpl_native
from types import FrameType
from docxtpl_native.bridge import BridgeConfig
original_file = inspect.__file__
original_code = inspect.isawaitable.__code__
seen: list[str] = []
def profile(frame: FrameType, event: str, argument: object) -> None:
    if event == 'call' and frame.f_code is original_code:
        seen.append('standard')
try:
    inspect.__file__ = None
    extension = docxtpl_native._extension()
    previous = sys.getprofile()
    try:
        sys.setprofile(profile)
        try:
            extension.PythonRenderSession(BridgeConfig(jinja2.Environment(enable_async=True), False),
                                          {'produce': lambda: object()}).render('case', '{{ produce() }}')
        except docxtpl_native.UnsupportedFeature as error:
            assert 'inspect.isawaitable provenance' in str(error), error
        else:
            raise AssertionError('expected conservative rejection')
    finally:
        sys.setprofile(previous)
    assert not seen, seen
finally:
    inspect.__file__ = original_file
''',
        )
        for script in scripts:
            with self.subTest(script=script[:40]):
                result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_runtime_auto_await_has_import_error(self) -> None:
        def observe(native: bool) -> tuple[str, str, object, object]:
            if native:
                docxtpl_native._extension()
            original = runtime.__dict__.pop('auto_await')
            environment = jinja2.Environment(enable_async=True)
            try:
                try:
                    if native:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {}
                        ).render('case', 'literal')
                    else:
                        environment.from_string('literal').render()
                except Exception as error:  # noqa: BLE001 - compare import error details
                    return type(error).__name__, str(error), getattr(error, 'name', None), getattr(error, 'path', None)
                raise AssertionError('expected missing runtime import to fail')
            finally:
                runtime.auto_await = original

        expected = observe(False)
        self.assertEqual(expected[0], 'ImportError')
        self.assertEqual(observe(True), expected)

    def test_missing_runtime_attribute_uses_import_module_fallback(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> str:
                events.append(str(value))
                return 'fallback'

            original = runtime.__dict__.pop('auto_await')
            environment = jinja2.Environment(enable_async=True)
            context = {'produce': lambda: 'x'}
            try:
                with patch.dict(sys.modules, {'jinja2.runtime.auto_await': replacement}):
                    if native:
                        output = docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), context
                        ).render('case', '{{ produce() }}')
                    else:
                        output = environment.from_string('{{ produce() }}').render(context)
                return output, events
            finally:
                runtime.auto_await = original

        expected = observe(False)
        self.assertEqual(expected, ('fallback', ['x']))
        self.assertEqual(observe(True), expected)

    def test_invalid_template_does_not_read_runtime_auto_await(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            original = runtime.__dict__.pop('auto_await')

            def missing(name: str) -> Any:
                events.append(name)
                raise AttributeError(name)

            environment = jinja2.Environment(enable_async=True)
            try:
                with patch.dict(runtime.__dict__, {'__getattr__': missing}):
                    try:
                        if native:
                            docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {}
                            ).render('case', '{% if %}')
                        else:
                            environment.from_string('{% if %}').render()
                    except jinja2.TemplateSyntaxError:
                        result = 'syntax'
                    else:
                        raise AssertionError('expected invalid syntax')
            finally:
                runtime.auto_await = original
            return result, events

        self.assertEqual(observe(False), ('syntax', []))
        self.assertEqual(observe(True), ('syntax', []))

    def test_noncallable_runtime_auto_await_error_is_preserved(self) -> None:
        def observe(native: bool) -> tuple[str, str]:
            environment = jinja2.Environment(enable_async=True)
            with patch.object(runtime, 'auto_await', 42):
                try:
                    if native:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {'produce': lambda: 'x'}
                        ).render('case', '{{ produce() }}')
                    else:
                        environment.from_string('{{ produce() }}').render(produce=lambda: 'x')
                except Exception as error:  # noqa: BLE001 - compare Python call error
                    return type(error).__name__, str(error)
            raise AssertionError('expected a noncallable error')

        self.assertEqual(observe(True), observe(False))

    def test_runtime_auto_await_returning_nonawaitable_preserves_error(self) -> None:
        def observe(native: bool) -> tuple[str, str]:
            environment = jinja2.Environment(enable_async=True)
            with patch.object(runtime, 'auto_await', lambda value: 42):
                try:
                    if native:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {'produce': lambda: 'x'}
                        ).render('case', '{{ produce() }}')
                    else:
                        environment.from_string('{{ produce() }}').render(produce=lambda: 'x')
                except Exception as error:  # noqa: BLE001 - compare await protocol error
                    return type(error).__name__, str(error)
            raise AssertionError('expected an invalid awaitable error')

        self.assertEqual(observe(True), observe(False))

    def test_runtime_auto_await_is_captured_for_each_template(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def first(value: object) -> object:
                events.append('first:' + str(value))
                return value

            async def second(value: object) -> object:
                events.append('second:' + str(value))
                return value

            def flip() -> str:
                setattr(runtime, 'auto_await', second)  # noqa: B010 - deliberate runtime mutation
                return ''

            environment = jinja2.Environment(enable_async=True, loader=jinja2.DictLoader({'part': '{{ produce() }}'}))
            context = {'flip': flip, 'produce': lambda: 'x'}
            source = "{{ flip() }}{% include 'part' %}{{ produce() }}"
            with patch.object(runtime, 'auto_await', first):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(context)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('xx', ['first:', 'second:x', 'first:x']))
        self.assertEqual(observe(True), expected)

    def test_runtime_auto_await_is_used_inside_macro_and_by_caller(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> object:
                if inspect.isawaitable(value):
                    value = await value
                events.append(str(value))
                return value

            environment = jinja2.Environment(enable_async=True)
            source = '{% macro m() %}{{ produce() }}{% endmacro %}{{ m() }}'
            context = {'produce': lambda: 'x'}
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(context)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('x', ['x', 'x']))
        self.assertEqual(observe(True), expected)

    def test_runtime_auto_await_is_used_for_super(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> object:
                if inspect.isawaitable(value):
                    value = await value
                events.append(str(value))
                return value

            environment = jinja2.Environment(enable_async=True,
                loader=jinja2.DictLoader({'base': '{% block body %}parent{% endblock %}'}))
            source = "{% extends 'base' %}{% block body %}{{ super() }}{% endblock %}"
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render()
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('parent', ['parent']))
        self.assertEqual(observe(True), expected)

    def test_runtime_auto_await_is_used_for_recursive_loop(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> object:
                if inspect.isawaitable(value):
                    value = await value
                events.append(str(value))
                return value

            environment = jinja2.Environment(enable_async=True)
            source = ('{% for x in nodes recursive %}{{ x.name }}'
                      '{% if x.children %}{{ loop(x.children) }}{% endif %}{% endfor %}')
            context = {'nodes': [{'name': 'a', 'children': [{'name': 'b', 'children': []}]}]}
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(context)
            return output, events

        expected = observe(False)
        self.assertEqual(expected[0], 'ab')
        self.assertEqual(observe(True), expected)

    def test_custom_auto_await_can_skip_recursive_body(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    events.append('coroutine')
                    value.close()
                    return 'skipped'
                return value

            def inner() -> str:
                events.append('inner')
                return 'unexpected'

            source = ('{% for x in [0] recursive %}{% if x == 0 %}{{ loop([1]) }}'
                      '{% else %}{{ inner() }}{% endif %}{% endfor %}')
            environment = jinja2.Environment(enable_async=True)
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'inner': inner}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render({'inner': inner})
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('skipped', ['coroutine']))
        self.assertEqual(observe(True), expected)

    def test_custom_auto_await_can_skip_super_body(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    events.append('coroutine')
                    value.close()
                    return 'skipped'
                return value

            def inner() -> str:
                events.append('inner')
                return 'unexpected'

            environment = jinja2.Environment(enable_async=True, loader=jinja2.DictLoader({
                'base': '{% block body %}{{ inner() }}{% endblock %}',
            }))
            source = "{% extends 'base' %}{% block body %}{{ super() }}{% endblock %}"
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'inner': inner}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render({'inner': inner})
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('skipped', ['coroutine']))
        self.assertEqual(observe(True), expected)

    def test_standard_auto_await_observes_recursive_coroutine_type(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            original = async_utils._common_primitives

            class Primitives:
                def __contains__(self, item: object) -> bool:
                    if item is CoroutineType:
                        events.append('coroutine')
                    return item in original

            source = ('{% for x in [0] recursive %}{% if x == 0 %}{{ loop([1]) }}'
                      '{% else %}nested{% endif %}{% endfor %}')
            environment = jinja2.Environment(enable_async=True)
            with patch.dict(async_utils.__dict__, {'_common_primitives': Primitives()}):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render()
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('nested', ['coroutine']))
        self.assertEqual(observe(True), expected)

    def test_custom_auto_await_catches_recursive_body_error(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            failure = ValueError('nested failure')

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    try:
                        return await value
                    except ValueError as error:
                        events.append('same' if error is failure else 'different')
                        return 'caught'
                return value

            def fail() -> str:
                events.append('fail')
                raise failure

            source = ('{% for x in [0] recursive %}{% if x == 0 %}{{ loop([1]) }}:{{ loop.depth }}'
                      '{% else %}partial{{ fail() }}{% endif %}{% endfor %}')
            environment = jinja2.Environment(enable_async=True)
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'fail': fail}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render({'fail': fail})
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('caught:1', ['fail', 'same']))
        self.assertEqual(observe(True), expected)

    def test_custom_auto_await_catches_invalid_recursive_iterable(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    try:
                        return await value
                    except TypeError as error:
                        events.append(str(error))
                        return 'caught'
                return value

            source = '{% for x in [0] recursive %}{{ loop(none) }}:{{ loop.depth }}{% endfor %}'
            environment = jinja2.Environment(enable_async=True)
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render()
            return output, events

        expected = observe(False)
        self.assertEqual(expected[0], 'caught:1')
        self.assertEqual(len(expected[1]), 1)
        self.assertEqual(observe(True), expected)

    def test_recursive_error_preserves_reference_autoescape_context(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    try:
                        return await value
                    except ValueError:
                        events.append('caught')
                        return 'caught'
                return value

            def fail() -> str:
                raise ValueError('nested failure')

            @jinja2.pass_eval_context
            def current(context: EvalContext) -> str:
                events.append(str(context.autoescape))
                return str(context.autoescape)

            source = ('{% for x in [0] recursive %}{% if x == 0 %}{{ loop([1]) }}:{{ current() }}'
                      '{% else %}{% autoescape true %}{{ fail() }}{% endautoescape %}'
                      '{% endif %}{% endfor %}')
            environment = jinja2.Environment(enable_async=True, autoescape=False)
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'fail': fail, 'current': current}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render({'fail': fail, 'current': current})
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('caught:True', ['caught', 'True']))
        self.assertEqual(observe(True), expected)

    def test_custom_auto_await_suspends_around_recursive_body(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    events.append('before-await')
                    await asyncio.sleep(0)
                    result = await value
                    events.append('after-await')
                    return result
                return value

            async def inner() -> str:
                events.append('inner-before')
                await asyncio.sleep(0)
                events.append('inner-after')
                return 'nested'

            source = ('{% for x in [0] recursive %}{% if x == 0 %}{{ loop([1]) }}'
                      '{% else %}{{ inner() }}{% endif %}{% endfor %}')
            environment = jinja2.Environment(enable_async=True)
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'inner': inner}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render({'inner': inner})
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('nested', [
            'before-await', 'before-await', 'inner-before', 'inner-after', 'after-await', 'after-await',
        ]))
        self.assertEqual(observe(True), expected)

    def test_recursive_coroutine_can_run_in_another_task(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    events.append('task')
                    return await asyncio.create_task(value)
                return value

            def inner() -> str:
                events.append('inner')
                return 'nested'

            source = ('{% for x in [0] recursive %}{% if x == 0 %}{{ loop([1]) }}'
                      '{% else %}{{ inner() }}{% endif %}{% endfor %}')
            environment = jinja2.Environment(enable_async=True)
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'inner': inner}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render({'inner': inner})
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('nested', ['task', 'inner']))
        self.assertEqual(observe(True), expected)

    def test_recursive_coroutine_can_be_awaited_after_render(self) -> None:
        def observe(native: bool) -> tuple[str, str, list[str]]:
            events: list[str] = []
            retained: list[Any] = []

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    retained.append(value)
                    return 'deferred'
                return value

            def inner() -> str:
                events.append('inner')
                return 'nested'

            source = ('{% for x in [0] recursive %}{% if x == 0 %}{{ loop([1]) }}'
                      '{% else %}{{ inner() }}{% endif %}{% endfor %}')
            environment = jinja2.Environment(enable_async=True)
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'inner': inner}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render({'inner': inner})
                self.assertEqual(events, [])
                later = asyncio.run(retained.pop())
            return output, later, events

        expected = observe(False)
        self.assertEqual(expected, ('deferred', 'nested', ['inner']))
        self.assertEqual(observe(True), expected)

    def test_recursive_awaitable_exposes_generated_coroutine_code_name(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            names: list[str] = []

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    names.append(value.cr_code.co_name)
                    return await value
                return value

            source = ('{% for x in [0] recursive %}{% if x == 0 %}{{ loop([1]) }}'
                      '{% else %}nested{% endif %}{% endfor %}')
            environment = jinja2.Environment(enable_async=True)
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render()
            return output, names

        expected = observe(False)
        self.assertEqual(expected, ('nested', ['loop']))
        self.assertEqual(observe(True), expected)

    def test_recursive_await_result_obeys_autoescape(self) -> None:
        def observe(native: bool) -> str:
            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    value.close()
                    return '<unsafe>'
                return value

            source = ('{% for x in [0] recursive %}{% if x == 0 %}{{ loop([1]) }}'
                      '{% else %}unused{% endif %}{% endfor %}')
            environment = jinja2.Environment(enable_async=True, autoescape=True)
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    return docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                return environment.from_string(source).render()

        self.assertEqual(observe(False), '&lt;unsafe&gt;')
        self.assertEqual(observe(True), '&lt;unsafe&gt;')

    def test_recursive_body_output_obeys_autoescape(self) -> None:
        def observe(native: bool) -> str:
            source = ('{% for x in [0] recursive %}{% if x == 0 %}{{ loop([1]) }}'
                      '{% else %}{{ value }}{% endif %}{% endfor %}')
            environment = jinja2.Environment(enable_async=True, autoescape=True)
            context = {'value': '<unsafe>'}
            if native:
                return docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context
                ).render('case', source)
            return environment.from_string(source).render(context)

        expected = observe(False)
        self.assertEqual(expected, '&lt;unsafe&gt;')
        self.assertEqual(observe(True), expected)

    def test_custom_auto_await_observes_recursive_result_type(self) -> None:
        def observe(native: bool, autoescape: bool) -> tuple[str, list[tuple[str, str]]]:
            events: list[tuple[str, str]] = []

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    result = await value
                    events.append((type(result).__name__, str(result)))
                    return result
                return value

            source = ('{% for x in [0] recursive %}{% if x == 0 %}{{ loop([1]) }}'
                      '{% else %}{{ value }}{% endif %}{% endfor %}')
            environment = jinja2.Environment(enable_async=True, autoescape=autoescape)
            context = {'value': '<unsafe>'}
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(context)
            return output, events

        for autoescape in (False, True):
            with self.subTest(autoescape=autoescape):
                self.assertEqual(observe(True, autoescape), observe(False, autoescape))

    def test_custom_auto_await_nested_recursive_depth(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    events.append('before')
                    result = await value
                    events.append('after')
                    return result
                return value

            source = ('{% for x in [0] recursive %}{{ loop.depth }}'
                      '{% if x < 2 %}[{{ loop([x + 1]) }}]{% endif %}{% endfor %}')
            environment = jinja2.Environment(enable_async=True)
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render()
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('1[2[3]]', ['before', 'before', 'after', 'after']))
        self.assertEqual(observe(True), expected)

    def test_custom_auto_await_catches_recursive_cancellation(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            failure = asyncio.CancelledError('nested cancellation')
            inside_recursive = False

            async def replacement(value: object) -> object:
                nonlocal inside_recursive
                if inspect.iscoroutine(value):
                    if inside_recursive:
                        return await value
                    inside_recursive = True
                    try:
                        return await value
                    except asyncio.CancelledError as error:
                        events.append('same' if error is failure else 'different')
                        return 'caught'
                    finally:
                        inside_recursive = False
                return value

            async def fail() -> str:
                events.append('fail')
                await asyncio.sleep(0)
                raise failure

            source = ('{% for x in [0] recursive %}{% if x == 0 %}{{ loop([1]) }}:{{ loop.depth }}'
                      '{% else %}partial{{ fail() }}{% endif %}{% endfor %}')
            environment = jinja2.Environment(enable_async=True)
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'fail': fail}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render({'fail': fail})
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('caught:1', ['fail', 'same']))
        self.assertEqual(observe(True), expected)

    def test_recursive_body_does_not_execute_reference_python_code(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            calls: list[str] = []
            environment = jinja2.Environment(enable_async=True)
            source = ('{% for x in [0] recursive %}{% if x == 0 %}{{ loop([1]) }}'
                      '{% else %}nested{% endif %}{% endfor %}')
            reference = environment.from_string(source)
            codes = {item for item in cast(Any, reference.root_render_func).__code__.co_consts
                     if isinstance(item, CodeType) and item.co_name == 'loop'}
            self.assertEqual(len(codes), 1)
            previous = sys.getprofile()

            def profile(frame: FrameType, event: str, argument: object) -> None:
                if event == 'call' and frame.f_code in codes:
                    calls.append('loop')

            try:
                sys.setprofile(profile)
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    output = reference.render()
            finally:
                sys.setprofile(previous)
            return output, calls

        reference, calls = observe(False)
        self.assertEqual(reference, 'nested')
        self.assertTrue(calls)
        self.assertEqual(observe(True), ('nested', []))

    def test_sandbox_rejects_recursive_call_before_auto_await(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> object:
                events.append('await')
                if inspect.iscoroutine(value):
                    return await value
                return value

            source = ('{% for x in [0] recursive %}{% if x == 0 %}{{ loop([1]) }}'
                      '{% else %}nested{% endif %}{% endfor %}')
            environment = SandboxedEnvironment(enable_async=True)
            with patch.object(runtime.AsyncLoopContext, 'unsafe_callable', True, create=True), \
                 patch.object(runtime, 'auto_await', replacement):
                try:
                    if native:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {}
                        ).render('case', source)
                    else:
                        environment.from_string(source).render()
                except SecurityError:
                    return 'rejected', events
            return 'allowed', events

        expected = observe(False)
        self.assertEqual(expected, ('rejected', []))
        self.assertEqual(observe(True), expected)

    def test_custom_auto_await_suspends_before_and_inside_super(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    events.append('before-await')
                    await asyncio.sleep(0)
                    result = await value
                    events.append('after-await')
                    return result
                return value

            async def inner() -> str:
                events.append('inner-before')
                await asyncio.sleep(0)
                events.append('inner-after')
                return 'parent'

            environment = jinja2.Environment(enable_async=True, loader=jinja2.DictLoader({
                'base': '{% block body %}{{ inner() }}{% endblock %}',
            }))
            source = "{% extends 'base' %}{% block body %}{{ super() }}{% endblock %}"
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'inner': inner}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render({'inner': inner})
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('parent', [
            'before-await', 'before-await', 'inner-before', 'inner-after', 'after-await', 'after-await',
        ]))
        self.assertEqual(observe(True), expected)

    def test_super_coroutine_can_be_awaited_after_render(self) -> None:
        def observe(native: bool) -> tuple[str, str, list[str]]:
            events: list[str] = []
            retained: list[Any] = []

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    retained.append(value)
                    return 'deferred'
                return value

            def inner() -> str:
                events.append('inner')
                return 'parent'

            environment = jinja2.Environment(enable_async=True, loader=jinja2.DictLoader({
                'base': '{% block body %}{{ inner() }}{% endblock %}',
            }))
            source = "{% extends 'base' %}{% block body %}{{ super() }}{% endblock %}"
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'inner': inner}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render({'inner': inner})
                self.assertEqual(events, [])
                later = asyncio.run(retained.pop())
            return output, later, events

        expected = observe(False)
        self.assertEqual(expected, ('deferred', 'parent', ['inner']))
        self.assertEqual(observe(True), expected)

    def test_super_coroutine_can_run_in_another_task(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    events.append('task')
                    return await asyncio.create_task(value)
                return value

            def inner() -> str:
                events.append('inner')
                return 'parent'

            environment = jinja2.Environment(enable_async=True, loader=jinja2.DictLoader({
                'base': '{% block body %}{{ inner() }}{% endblock %}',
            }))
            source = "{% extends 'base' %}{% block body %}{{ super() }}{% endblock %}"
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'inner': inner}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render({'inner': inner})
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('parent', ['task', 'inner']))
        self.assertEqual(observe(True), expected)

    def test_custom_auto_await_error_prevents_super_body(self) -> None:
        def observe(native: bool) -> tuple[bool, list[str]]:
            events: list[str] = []
            failure = ValueError('await rejected')

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    value.close()
                    events.append('rejected')
                    raise failure
                return value

            def inner() -> str:
                events.append('inner')
                return 'unexpected'

            environment = jinja2.Environment(enable_async=True, loader=jinja2.DictLoader({
                'base': '{% block body %}{{ inner() }}{% endblock %}',
            }))
            source = "{% extends 'base' %}{% block body %}{{ super() }}{% endblock %}"
            with patch.object(runtime, 'auto_await', replacement):
                try:
                    if native:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {'inner': inner}
                        ).render('case', source)
                    else:
                        environment.from_string(source).render({'inner': inner})
                except ValueError as error:
                    return error is failure, events
            self.fail('expected await callback failure')

        expected = observe(False)
        self.assertEqual(expected, (True, ['rejected']))
        self.assertEqual(observe(True), expected)

    def test_custom_auto_await_can_call_macro_before_super(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            saved: list[Any] = []

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    events.append(await saved[0]())
                    return await value
                return value

            def capture(value: object) -> str:
                saved.append(value)
                return ''

            environment = jinja2.Environment(enable_async=True, loader=jinja2.DictLoader({
                'base': '{% block body %}parent{% endblock %}',
            }))
            source = ("{% extends 'base' %}{% macro item() %}macro{% endmacro %}"
                      "{% block body %}{{ capture(item) }}{{ super() }}{% endblock %}")
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'capture': capture}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render({'capture': capture})
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('parent', ['macro']))
        self.assertEqual(observe(True), expected)

    def test_macro_called_before_super_can_suspend(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            saved: list[Any] = []
            inside_macro = False

            async def replacement(value: object) -> object:
                nonlocal inside_macro
                if not inspect.iscoroutine(value):
                    return value
                if inside_macro:
                    return await value
                inside_macro = True
                try:
                    result = await saved[0]()
                    events.append('macro:' + result)
                finally:
                    inside_macro = False
                return await value

            def capture(value: object) -> str:
                saved.append(value)
                return ''

            async def wait() -> str:
                events.append('before')
                await asyncio.sleep(0)
                events.append('after')
                return 'result'

            environment = jinja2.Environment(enable_async=True, loader=jinja2.DictLoader({
                'base': '{% block body %}parent{% endblock %}',
            }))
            source = ("{% extends 'base' %}{% macro item() %}{{ wait() }}{% endmacro %}"
                      "{% block body %}{{ capture(item) }}{{ super() }}{% endblock %}")
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'capture': capture, 'wait': wait}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render({'capture': capture, 'wait': wait})
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('parent', ['before', 'after', 'macro:result']))
        self.assertEqual(observe(True), expected)

    def test_super_awaitable_exposes_reference_coroutine_code(self) -> None:
        def observe(native: bool) -> tuple[str, list[bool]]:
            seen: list[bool] = []

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    seen.append(value.cr_code is runtime.BlockReference._async_call.__code__)
                    return await value
                return value

            environment = jinja2.Environment(enable_async=True, loader=jinja2.DictLoader({
                'base': '{% block body %}parent{% endblock %}',
            }))
            source = "{% extends 'base' %}{% block body %}{{ super() }}{% endblock %}"
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render()
            return output, seen

        expected = observe(False)
        self.assertEqual(expected, ('parent', [True]))
        self.assertEqual(observe(True), expected)

    def test_custom_auto_await_nested_super_chain(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    events.append('before')
                    result = await value
                    events.append('after')
                    return result
                return value

            environment = jinja2.Environment(enable_async=True, loader=jinja2.DictLoader({
                'base': '{% block body %}A{% endblock %}',
                'mid': "{% extends 'base' %}{% block body %}B[{{ super() }}]{% endblock %}",
            }))
            source = "{% extends 'mid' %}{% block body %}C[{{ super() }}]{% endblock %}"
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render()
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('C[B[A]]', ['before', 'before', 'after', 'after']))
        self.assertEqual(observe(True), expected)

    def test_standard_auto_await_observes_super_coroutine_type(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            original = async_utils._common_primitives

            class Primitives:
                def __contains__(self, item: object) -> bool:
                    if item is CoroutineType:
                        events.append('coroutine')
                    return item in original

            environment = jinja2.Environment(enable_async=True, loader=jinja2.DictLoader({
                'base': '{% block body %}parent{% endblock %}',
            }))
            source = "{% extends 'base' %}{% block body %}{{ super() }}{% endblock %}"
            with patch.dict(async_utils.__dict__, {'_common_primitives': Primitives()}):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render()
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('parent', ['coroutine']))
        self.assertEqual(observe(True), expected)

    def test_super_block_body_does_not_execute_reference_async_call(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            calls: list[str] = []
            previous = sys.getprofile()
            code = runtime.BlockReference._async_call.__code__

            def profile(frame: FrameType, event: str, argument: object) -> None:
                if event == 'call' and frame.f_code is code:
                    calls.append('reference')

            environment = jinja2.Environment(enable_async=True, loader=jinja2.DictLoader({
                'base': '{% block body %}parent{% endblock %}',
            }))
            source = "{% extends 'base' %}{% block body %}{{ super() }}{% endblock %}"
            try:
                sys.setprofile(profile)
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render()
            finally:
                sys.setprofile(previous)
            return output, calls

        reference, calls = observe(False)
        self.assertEqual(reference, 'parent')
        self.assertTrue(calls)
        self.assertEqual(observe(True), ('parent', []))

    def test_custom_auto_await_super_return_value_obeys_autoescape(self) -> None:
        def observe(native: bool) -> str:
            async def replacement(value: object) -> object:
                if inspect.iscoroutine(value):
                    value.close()
                    return '<unsafe>'
                return value

            environment = jinja2.Environment(enable_async=True, autoescape=True, loader=jinja2.DictLoader({
                'base': '{% block body %}parent{% endblock %}',
            }))
            source = "{% extends 'base' %}{% block body %}{{ super() }}{% endblock %}"
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    return docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                return environment.from_string(source).render()

        self.assertEqual(observe(False), '&lt;unsafe&gt;')
        self.assertEqual(observe(True), '&lt;unsafe&gt;')

    def test_custom_auto_await_recursive_loop_executes_nested_body_when_awaited(self) -> None:
        events: list[str] = []

        async def replacement(value: object) -> object:
            if inspect.isawaitable(value):
                return await value
            return value

        def make() -> list[int]:
            events.append('make')
            return [1]

        def inner() -> str:
            events.append('inner')
            return 'x'

        source = ('{% for x in [0] recursive %}{% if x == 0 %}{{ loop(make()) }}'
                  '{% else %}{{ inner() }}{% endif %}{% endfor %}')
        environment = jinja2.Environment(enable_async=True)
        with patch.object(runtime, 'auto_await', replacement):
            result = docxtpl_native._extension().PythonRenderSession(
                BridgeConfig(environment, False), {'make': make, 'inner': inner}
            ).render('case', source)
        self.assertEqual(result, 'x')
        self.assertEqual(events, ['make', 'inner'])

    def test_cached_include_retains_compiled_auto_await(self) -> None:
        def observe(native: bool) -> tuple[list[str], list[str]]:
            events: list[str] = []

            async def first(value: object) -> object:
                events.append('first:' + str(value))
                return value

            async def second(value: object) -> object:
                events.append('second:' + str(value))
                return value

            environment = jinja2.Environment(enable_async=True, loader=jinja2.DictLoader({'part': '{{ produce() }}'}))
            context = {'produce': lambda: 'x'}
            source = "{% include 'part' %}"
            session: Any = None
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)

            def render() -> str:
                return session.render('case', source) if native else environment.from_string(source).render(context)

            with patch.object(runtime, 'auto_await', first):
                first_output = render()
            with patch.object(runtime, 'auto_await', second):
                second_output = render()
            return [first_output, second_output], events

        expected = observe(False)
        self.assertEqual(expected, (['x', 'x'], ['first:x', 'first:x']))
        self.assertEqual(observe(True), expected)

    def test_runtime_auto_await_is_captured_after_constant_folding(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def first(value: object) -> object:
                events.append('first:' + str(value))
                return value

            async def second(value: object) -> object:
                events.append('second:' + str(value))
                return value

            def change(value: str) -> str:
                events.append('fold')
                setattr(runtime, 'auto_await', second)  # noqa: B010 - deliberate compilation mutation
                return value

            environment = jinja2.Environment(enable_async=True)
            environment.filters['change'] = change
            source = "{{ 'folded'|change }}{{ produce() }}"
            context = {'produce': lambda: 'x'}
            with patch.object(runtime, 'auto_await', first):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(context)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('foldedx', ['fold', 'second:x']))
        self.assertEqual(observe(True), expected)

    def test_runtime_auto_await_replacement_before_compilation_is_used(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> str:
                events.append('custom:' + str(value))
                return 'replaced'

            environment = jinja2.Environment(enable_async=True)
            context = {'produce': lambda: 'original'}
            with patch.object(runtime, 'auto_await', replacement):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context
                    ).render('case', '{{ produce() }}')
                else:
                    output = environment.from_string('{{ produce() }}').render(context)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('replaced', ['custom:original']))
        self.assertEqual(observe(True), expected)

    def test_isawaitable_preserves_live_truth_protocol(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Value:
                def __await__(self) -> Generator[None, None, str]:
                    events.append('await')
                    if False:
                        yield None
                    return 'done'

            class Decision:
                def __bool__(self) -> bool:
                    events.append('bool')
                    return True

            value = Value()

            def instance(item: object, kind: Any) -> object:
                if item is value:
                    events.append('check:' + kind.__name__)
                    return Decision()
                return builtins.isinstance(item, kind)

            environment = jinja2.Environment(enable_async=True)
            context = {'produce': lambda: value}
            with patch.dict(inspect.__dict__, {'isinstance': instance}):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context
                    ).render('case', '{{ produce() }}')
                else:
                    output = environment.from_string('{{ produce() }}').render(context)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('done', ['check:coroutine', 'bool', 'bool', 'await']))
        self.assertEqual(observe(True), expected)

    def test_standard_await_detection_runs_without_standard_python_bodies(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            codes = {async_utils.auto_await.__code__, inspect.isawaitable.__code__}

            def profile(frame: Any, event: str, argument: Any) -> None:
                if event == 'call' and frame.f_code in codes:
                    events.append(frame.f_code.co_name)

            environment = jinja2.Environment(enable_async=True)
            context = {'produce': lambda: object()}
            previous = sys.getprofile()
            try:
                sys.setprofile(profile)
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context
                    ).render('case', '{{ produce() is not none }}')
                else:
                    output = environment.from_string('{{ produce() is not none }}').render(context)
            finally:
                sys.setprofile(previous)
            return output, events

        expected, reference_calls = observe(False)
        actual, candidate_calls = observe(True)
        self.assertEqual(expected, 'True')
        self.assertEqual(actual, expected)
        self.assertIn('auto_await', reference_calls)
        self.assertIn('isawaitable', reference_calls)
        self.assertEqual(candidate_calls, [])

    def test_in_place_auto_await_override_after_initialization_is_user_callback(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(value: object) -> str:
                globals()['_await_test_events'].append('custom:' + str(value))
                return 'custom-result'

            original = async_utils.auto_await.__code__
            if native:
                docxtpl_native._extension()
            environment = jinja2.Environment(enable_async=True)
            context = {'produce': lambda: 'original'}
            try:
                with patch.dict(async_utils.__dict__, {'_await_test_events': events}):
                    async_utils.auto_await.__code__ = replacement.__code__
                    if native:
                        output = docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), context
                        ).render('case', '{{ produce() }}')
                    else:
                        output = environment.from_string('{{ produce() }}').render(context)
            finally:
                async_utils.auto_await.__code__ = original
            return output, events

        self.assertEqual(observe(True), observe(False))

    def test_common_primitive_and_cast_callbacks_are_live(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Primitives:
                def __contains__(self, value: object) -> bool:
                    events.append('contains:' + getattr(value, '__name__', '?'))
                    return value is str

            def value_type(value: object) -> type[object]:
                events.append('type:' + type(value).__name__)
                return type(value)

            def cast(name: object, value: object) -> object:
                events.append('cast:' + str(name))
                return 'changed'

            environment = jinja2.Environment(enable_async=True)
            context = {'produce': lambda: 'original'}
            with patch.dict(async_utils.__dict__, {'_common_primitives': Primitives(), 'type': value_type,
                                                   't': SimpleNamespace(cast=cast)}):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context
                    ).render('case', '{{ produce() }}')
                else:
                    output = environment.from_string('{{ produce() }}').render(context)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('changed', ['type:str', 'contains:str', 'cast:V']))
        self.assertEqual(observe(True), expected)

    def test_replaced_isawaitable_can_prevent_await(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Value:
                def __await__(self) -> Generator[None, None, str]:
                    events.append('await')
                    if False:
                        yield None
                    return 'awaited'

                def __str__(self) -> str:
                    events.append('str')
                    return 'original'

            value = Value()

            def isawaitable(item: object) -> bool:
                events.append('isawaitable:' + str(item is value))
                return False

            environment = jinja2.Environment(enable_async=True)
            context = {'produce': lambda: value}
            with patch.dict(async_utils.__dict__, {'inspect': SimpleNamespace(isawaitable=isawaitable)}):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context
                    ).render('case', '{{ produce() }}')
                else:
                    output = environment.from_string('{{ produce() }}').render(context)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('original', ['isawaitable:True', 'str']))
        self.assertEqual(observe(True), expected)

    def test_cast_can_replace_awaited_object(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Value:
                def __init__(self, label: str) -> None:
                    self.label = label

                def __await__(self) -> Generator[None, None, str]:
                    events.append('await:' + self.label)
                    if False:
                        yield None
                    return self.label

            original, alternate = Value('original'), Value('alternate')

            def cast(name: object, value: object) -> object:
                events.append('cast:' + str(name) + ':' + str(value is original))
                return alternate

            environment = jinja2.Environment(enable_async=True)
            context = {'produce': lambda: original}
            with patch.dict(async_utils.__dict__, {'t': SimpleNamespace(cast=cast)}):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context
                    ).render('case', '{{ produce() }}')
                else:
                    output = environment.from_string('{{ produce() }}').render(context)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('alternate', ['cast:t.Awaitable[V]:True', 'await:alternate']))
        self.assertEqual(observe(True), expected)

    def test_common_primitive_callback_exception_preserves_identity(self) -> None:
        for native in (False, True):
            with self.subTest(native=native):
                failure = ValueError('membership failed')

                class Primitives:
                    def __init__(self, error: ValueError) -> None:
                        self.error = error

                    def __contains__(self, value: object) -> bool:
                        raise self.error

                environment = jinja2.Environment(enable_async=True)
                context: dict[str, Any] = {'produce': lambda: 'original'}
                with patch.dict(async_utils.__dict__, {'_common_primitives': Primitives(failure)}), \
                     self.assertRaises(ValueError) as caught:
                    if native:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), context
                        ).render('case', '{{ produce() }}')
                    else:
                        environment.from_string('{{ produce() }}').render(context)
                self.assertIs(caught.exception, failure)
