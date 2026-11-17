"""Sandbox attribute decisions and their Python protocol boundary."""

import asyncio
import builtins
import inspect
import io
import subprocess
import sys
import textwrap
import unittest
from types import FrameType
from typing import Any, cast
from unittest.mock import patch

import docxtpl
import docxtpl_native
from jinja2 import sandbox
from jinja2.sandbox import SandboxedEnvironment
from paritylab.compare import compare_docx
from paritylab.protocol_base import template


def decide(native: bool, environment: SandboxedEnvironment, obj: object, attr: object, value: object) -> Any:
    if native:
        return docxtpl_native._extension().python_is_safe_attribute(environment, obj, attr, value)
    return cast(Any, environment).is_safe_attribute(obj, attr, value)


class NativeSandboxAttributeTests(unittest.TestCase):
    def test_public_file_standard_security_bodies_are_native(self) -> None:
        codes = {SandboxedEnvironment.getitem.__code__,
                 SandboxedEnvironment.is_safe_attribute.__code__, sandbox.is_internal_attribute.__code__}
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def run(engine: Any, asynchronous: bool = asynchronous) -> tuple[bytes, list[str]]:
                    calls: list[str] = []

                    class Value:
                        public = 'visible'
                        _private = 'secret'

                    def profile(frame: FrameType, event: str, argument: object) -> None:
                        if event == 'call' and frame.f_code in codes:
                            calls.append(frame.f_code.co_name)

                    source = template("{{ obj['public'] }}|{{ obj['_private']|default('blocked', true) }}")
                    document = engine.DocxTemplate(io.BytesIO(source))
                    child = object.__new__(docxtpl_native.Subdoc)
                    child.tpl = None
                    child.source = io.BytesIO(template('unused'))
                    previous = sys.getprofile()
                    try:
                        sys.setprofile(profile)
                        document.render({'obj': Value(), 'child': child},
                                        SandboxedEnvironment(enable_async=asynchronous))
                    finally:
                        sys.setprofile(previous)
                    output = io.BytesIO()
                    document.save(output)
                    return output.getvalue(), calls

                expected, reference_calls = run(docxtpl)
                actual, candidate_calls = run(docxtpl_native)
                self.assertTrue(compare_docx(expected, actual)['equivalent'])
                self.assertIn('getitem', reference_calls)
                self.assertIn('is_safe_attribute', reference_calls)
                self.assertIn('is_internal_attribute', reference_calls)
                self.assertEqual(candidate_calls, [])

    def test_internal_object_categories(self) -> None:
        def generator() -> Any:
            yield 1

        async def coroutine() -> None:
            return None

        async def async_generator() -> Any:
            yield 1

        iterator = generator()
        async_iterator = async_generator()
        awaitable = coroutine()
        try:
            objects = [lambda: None, ''.upper, str, generator.__code__, inspect.currentframe(),
                       iterator, awaitable, async_iterator, object()]
            attributes = ('public', 'mro', '__class__', 'gi_code', 'gi_frame', 'cr_code',
                          'cr_frame', 'ag_code', 'ag_frame', '_private')
            for obj in objects:
                for attribute in attributes:
                    with self.subTest(kind=type(obj).__name__, attribute=attribute):
                        env = SandboxedEnvironment()
                        self.assertEqual(decide(True, env, obj, attribute, None),
                                         decide(False, env, obj, attribute, None))
        finally:
            iterator.close()
            awaitable.close()
            asyncio.run(async_iterator.aclose())

    def test_stateful_startswith_truth_is_observed_twice(self) -> None:
        def run(native: bool) -> tuple[object, list[str]]:
            events: list[str] = []

            class Decision:
                def __bool__(self) -> bool:
                    events.append('bool')
                    return len(events) == 2

            class Attribute:
                def startswith(self, prefix: str) -> object:
                    events.append(f'startswith:{prefix}')
                    return Decision()

            return decide(native, SandboxedEnvironment(), object(), Attribute(), None), events

        self.assertEqual(run(True), run(False))

    def test_startswith_truth_exception_identity_is_preserved(self) -> None:
        def run(native: bool) -> tuple[bool, list[str]]:
            events: list[str] = []
            marker = ValueError('truth failure')

            class Decision:
                def __bool__(self) -> bool:
                    events.append('bool')
                    raise marker

            class Attribute:
                def startswith(self, prefix: str) -> object:
                    events.append(prefix)
                    return Decision()

            try:
                decide(native, SandboxedEnvironment(), object(), Attribute(), None)
            except ValueError as error:
                return error is marker, events
            raise AssertionError('expected truth failure')

        self.assertEqual(run(True), run(False))

    def test_replaced_internal_policy_and_contains_are_callbacks(self) -> None:
        def run(native: bool) -> tuple[list[object], list[tuple[str, object]]]:
            events: list[tuple[str, object]] = []

            def replaced(obj: object, attribute: object) -> object:
                events.append(('internal', attribute))
                return False

            class Unsafe:
                def __contains__(self, value: object) -> bool:
                    events.append(('contains', value))
                    return value == 'blocked'

            environment = SandboxedEnvironment()
            with patch.dict(sandbox.__dict__, {'is_internal_attribute': replaced}):
                first = decide(native, environment, object(), 'public', None)
            with patch.dict(sandbox.__dict__, {'UNSAFE_FUNCTION_ATTRIBUTES': Unsafe()}):
                second = decide(native, environment, lambda: None, 'blocked', None)
            return [first, second], events

        self.assertEqual(run(True), run(False))

    def test_replaced_internal_policy_result_truth_protocol(self) -> None:
        def run(native: bool) -> tuple[object, list[str]]:
            events: list[str] = []

            class Decision:
                def __bool__(self) -> bool:
                    events.append('bool')
                    return True

            def policy(obj: object, attribute: object) -> object:
                events.append('policy')
                return Decision()

            with patch.dict(sandbox.__dict__, {'is_internal_attribute': policy}):
                result = decide(native, SandboxedEnvironment(), object(), 'public', None)
            return result, events

        self.assertEqual(run(True), run(False))

    def test_types_global_is_resolved_again_after_a_callback(self) -> None:
        import types

        def run(native: bool) -> tuple[object, list[tuple[str, str]]]:
            events: list[tuple[str, str]] = []

            class TypesProxy:
                def __init__(self, label: str) -> None:
                    self.label = label

                def __getattr__(self, name: str) -> object:
                    events.append((self.label, name))
                    if self.label == 'first' and name == 'CodeType':
                        cast(Any, sandbox).types = second
                    return getattr(types, name)

            first = TypesProxy('first')
            second = TypesProxy('second')
            with patch.dict(sandbox.__dict__, {'types': first}):
                result = decide(native, SandboxedEnvironment(), object(), 'public', None)
            return result, events

        self.assertEqual(run(True), run(False))

    def test_replaced_types_module_lifetime_matches_reference(self) -> None:
        import types

        def run(native: bool) -> tuple[object, list[str]]:
            events: list[str] = []
            original = sandbox.types

            class TypesProxy:
                def __getattr__(self, name: str) -> object:
                    events.append(f'type:{name}')
                    return getattr(types, name)

                def __del__(self) -> None:
                    events.append('drop-types')

            def custom_isinstance(obj: object, kind: object) -> bool:
                events.append('isinstance-before')
                sandbox.types = types
                events.append('isinstance-after')
                return False

            try:
                with patch.dict(sandbox.__dict__, {'isinstance': custom_isinstance}):
                    cast(Any, sandbox).types = TypesProxy()
                    result = decide(native, SandboxedEnvironment(), object(), 'public', None)
            finally:
                sandbox.types = original
            return result, events

        self.assertEqual(run(True), run(False))

    def test_cold_start_does_not_probe_temporary_types_and_recovers(self) -> None:
        code = textwrap.dedent('''
            import docxtpl_native
            import jinja2
            from jinja2 import sandbox

            original = sandbox.types
            events: list[str] = []

            class Probe:
                def __getattr__(self, name: str) -> object:
                    events.append(name)
                    return getattr(original, name)

            sandbox.types = Probe()
            try:
                docxtpl_native._extension()
            finally:
                sandbox.types = original
            assert events == [], events
            from docxtpl_native.bridge import BridgeConfig
            environment = sandbox.SandboxedEnvironment()
            session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            assert session.render('case', '{% for value in [1] %}{{ loop.index }}{% endfor %}') == '1'
        ''')
        result = subprocess.run([sys.executable, '-c', code], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cold_start_recovers_when_runtime_sys_is_temporarily_replaced(self) -> None:
        code = textwrap.dedent('''
            import docxtpl_native
            from jinja2 import runtime, sandbox

            original = runtime.sys
            runtime.sys = None
            try:
                docxtpl_native._extension()
            finally:
                runtime.sys = original
            from docxtpl_native.bridge import BridgeConfig
            session = docxtpl_native._extension().PythonRenderSession(
                BridgeConfig(sandbox.SandboxedEnvironment(), False), {})
            assert session.render('case', '{% for value in [1] %}{{ loop.index }}{% endfor %}') == '1'
        ''')
        result = subprocess.run([sys.executable, '-c', code], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_custom_method_and_exception_identity(self) -> None:
        def run(native: bool) -> tuple[bool, list[str]]:
            marker = ValueError('policy error')
            events: list[str] = []

            class Environment(SandboxedEnvironment):
                def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
                    events.append(attr)
                    raise marker

            try:
                decide(native, Environment(), object(), 'public', None)
            except ValueError as error:
                return error is marker, events
            raise AssertionError('expected policy error')

        self.assertEqual(run(True), run(False))

    def test_standard_policy_bodies_are_native(self) -> None:
        codes = {SandboxedEnvironment.is_safe_attribute.__code__, sandbox.is_internal_attribute.__code__}

        def run(native: bool) -> tuple[object, list[str]]:
            calls: list[str] = []
            previous = sys.getprofile()

            def profile(frame: FrameType, event: str, argument: object) -> None:
                if event == 'call' and frame.f_code in codes:
                    calls.append(frame.f_code.co_name)

            try:
                sys.setprofile(profile)
                result = decide(native, SandboxedEnvironment(), lambda: None, 'public', None)
            finally:
                sys.setprofile(previous)
            return result, calls

        expected, reference_calls = run(False)
        actual, candidate_calls = run(True)
        self.assertEqual(actual, expected)
        self.assertTrue(reference_calls)
        self.assertEqual(candidate_calls, [])

    def test_loaded_bridge_is_not_reimported_during_policy(self) -> None:
        extension = docxtpl_native._extension()
        environment = SandboxedEnvironment()
        extension.python_is_safe_attribute(environment, object(), 'public', None)
        original = builtins.__import__
        events: list[str] = []

        def importer(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == 'docxtpl_native.bridge':
                events.append(name)
            return original(name, *args, **kwargs)

        with patch.object(builtins, '__import__', importer):
            self.assertTrue(extension.python_is_safe_attribute(environment, object(), 'public', None))
        self.assertEqual(events, [])
