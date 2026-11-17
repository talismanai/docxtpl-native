"""Sandbox item lookup: policy callbacks, exceptions and native boundary."""

import io
import sys
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


def lookup(native: bool, environment: SandboxedEnvironment, obj: object, key: object) -> Any:
    if native:
        return docxtpl_native._extension().python_lookup(environment, obj, key, False)
    return environment.getitem(obj, key)


class NativeSandboxGetitemTests(unittest.TestCase):
    def test_public_file_render_preserves_sandbox_policy(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def run(engine: Any, asynchronous: bool = asynchronous) -> tuple[bytes, list[tuple[str, str]], int]:
                    events: list[tuple[str, str]] = []
                    calls = 0
                    code = SandboxedEnvironment.getitem.__code__

                    class Value:
                        public = 'visible'
                        _private = 'secret'

                    class Environment(SandboxedEnvironment):
                        def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
                            events.append((attr, str(value)))
                            return super().is_safe_attribute(obj, attr, value)

                    def profile(frame: FrameType, event: str, argument: object) -> None:
                        nonlocal calls
                        if event == 'call' and frame.f_code is code:
                            calls += 1

                    source = template("{{ obj['public'] }}|{{ obj['_private']|default('blocked', true) }}")
                    document = engine.DocxTemplate(io.BytesIO(source))
                    child = object.__new__(docxtpl_native.Subdoc)
                    child.tpl = None
                    child.source = io.BytesIO(template('unused'))
                    previous = sys.getprofile()
                    try:
                        sys.setprofile(profile)
                        document.render({'obj': Value(), 'child': child},
                                        Environment(enable_async=asynchronous))
                    finally:
                        sys.setprofile(previous)
                    output = io.BytesIO()
                    document.save(output)
                    return output.getvalue(), events, calls

                expected, expected_events, reference_calls = run(docxtpl)
                actual, actual_events, candidate_calls = run(docxtpl_native)
                self.assertEqual(actual_events, expected_events)
                self.assertTrue(compare_docx(expected, actual)['equivalent'])
                self.assertGreater(reference_calls, 0)
                self.assertEqual(candidate_calls, 0)

    def test_policy_calls_and_result_identity(self) -> None:
        for mode in ('item', 'safe', 'unsafe', 'format', 'missing'):
            with self.subTest(mode=mode):
                def run(native: bool, mode: str = mode) -> tuple[list[tuple[str, object]], bool]:
                    events: list[tuple[str, object]] = []
                    sentinel = object()
                    wrapped = object()
                    blocked = object()
                    missing = object()

                    class Value:
                        public = sentinel

                        def __getitem__(self, key: object) -> object:
                            events.append(('item', key))
                            if mode == 'item':
                                return sentinel
                            raise KeyError(key)

                    class Environment(SandboxedEnvironment):
                        def wrap_str_format(self, value: object) -> Any:
                            events.append(('format', value is sentinel))
                            return wrapped if mode == 'format' else None

                        def is_safe_attribute(self, obj: object, attr: str, value: object) -> bool:
                            events.append(('safe', attr))
                            return mode != 'unsafe'

                        def unsafe_undefined(self, obj: object, attribute: str) -> Any:
                            events.append(('unsafe', attribute))
                            return blocked

                    environment = Environment()

                    def undefined(*, obj: object, name: object) -> object:
                        events.append(('undefined', name))
                        return missing

                    cast(Any, environment).undefined = undefined
                    key = 'absent' if mode == 'missing' else 'public'
                    result = lookup(native, environment, Value(), key)
                    expected = {'item': sentinel, 'safe': sentinel, 'unsafe': blocked,
                                'format': wrapped, 'missing': missing}[mode]
                    return events, result is expected

                self.assertEqual(run(True), run(False))

    def test_exception_identity_and_context(self) -> None:
        for phase in ('attribute', 'format', 'policy', 'unsafe', 'undefined'):
            with self.subTest(phase=phase):
                def run(native: bool, phase: str = phase) -> tuple[bool, bool, list[str]]:
                    events: list[str] = []
                    original = KeyError('original item error')
                    failure = ValueError('callback failed')

                    class Value:
                        def __getitem__(self, key: object) -> object:
                            events.append('item')
                            raise original

                        def __getattribute__(self, name: str) -> Any:
                            if name == 'x':
                                events.append('attribute')
                                if phase == 'attribute':
                                    raise failure
                                if phase == 'undefined':
                                    raise AttributeError(name)
                                return 'value'
                            return object.__getattribute__(self, name)

                    class Environment(SandboxedEnvironment):
                        def wrap_str_format(self, value: object) -> Any:
                            events.append('format')
                            if phase == 'format':
                                raise failure
                            return None

                        def is_safe_attribute(self, obj: object, attr: str, value: object) -> bool:
                            events.append('policy')
                            if phase == 'policy':
                                raise failure
                            return False

                        def unsafe_undefined(self, obj: object, attribute: str) -> Any:
                            events.append('unsafe')
                            raise failure

                    environment = Environment()

                    def undefined(*, obj: object, name: object) -> object:
                        events.append('undefined')
                        raise failure

                    cast(Any, environment).undefined = undefined
                    try:
                        lookup(native, environment, Value(), 'x')
                    except ValueError as error:
                        return error is failure, error.__context__ is original, events
                    raise AssertionError('expected callback failure')

                expected = run(False)
                self.assertTrue(expected[0])
                self.assertEqual(run(True), expected)

    def test_item_exceptions_outside_catch_tuple_propagate(self) -> None:
        for error_type in (AttributeError, RuntimeError):
            with self.subTest(error_type=error_type):
                def run(native: bool, error_type: type[Exception] = error_type) -> tuple[bool, list[str]]:
                    marker = error_type('item failed')
                    events: list[str] = []

                    class Value:
                        def __getitem__(self, key: object) -> object:
                            events.append('item')
                            raise marker

                        def __getattribute__(self, name: str) -> Any:
                            events.append('attribute')
                            return object.__getattribute__(self, name)

                    try:
                        lookup(native, SandboxedEnvironment(), Value(), 'x')
                    except error_type as error:
                        return error is marker, events
                    raise AssertionError('expected item failure')

                expected = run(False)
                self.assertEqual(expected, (True, ['item']))
                self.assertEqual(run(True), expected)

    def test_sandbox_exception_global_replacement_is_respected(self) -> None:
        class CustomLookup(Exception):
            pass

        def run(native: bool) -> tuple[bool, list[str]]:
            marker = KeyError('not in replacement catch tuple')
            events: list[str] = []

            class Value:
                def __getitem__(self, key: object) -> object:
                    events.append('item')
                    raise marker

            with patch.dict(sandbox.__dict__, {'LookupError': CustomLookup}):
                try:
                    lookup(native, SandboxedEnvironment(), Value(), 'x')
                except KeyError as error:
                    return error is marker, events
            raise AssertionError('expected original item failure')

        self.assertEqual(run(True), run(False))

    def test_live_globals_and_method_override(self) -> None:
        def run(native: bool) -> tuple[object, list[tuple[str, object]]]:
            events: list[tuple[str, object]] = []

            class Value:
                x = 'value'

                def __getitem__(self, key: object) -> object:
                    raise KeyError(key)

            environment = SandboxedEnvironment()

            def custom_isinstance(value: object, kind: object) -> bool:
                events.append(('isinstance', type(value).__name__))
                return True

            def custom_format(value: object) -> object:
                events.append(('format', value))
                return None

            cast(Any, environment).wrap_str_format = custom_format
            with patch.dict(sandbox.__dict__, {'isinstance': custom_isinstance}):
                result = lookup(native, environment, Value(), 'x')
            return result, events

        self.assertEqual(run(True), run(False))

    def test_standard_getitem_body_is_not_executed(self) -> None:
        code = SandboxedEnvironment.getitem.__code__

        class Value:
            public = 'value'

        def run(native: bool) -> tuple[object, list[str]]:
            calls: list[str] = []
            previous = sys.getprofile()

            def profile(frame: FrameType, event: str, argument: object) -> None:
                if event == 'call' and frame.f_code is code:
                    calls.append(frame.f_code.co_name)

            try:
                sys.setprofile(profile)
                result = lookup(native, SandboxedEnvironment(), Value(), 'public')
            finally:
                sys.setprofile(previous)
            return result, calls

        expected, reference_calls = run(False)
        actual, candidate_calls = run(True)
        self.assertEqual(actual, expected)
        self.assertTrue(reference_calls)
        self.assertEqual(candidate_calls, [])
