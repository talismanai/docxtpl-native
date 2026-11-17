"""Sandbox attribute lookup: policy callbacks, errors and native execution."""

import builtins
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
        return docxtpl_native._extension().python_lookup(environment, obj, key, True)
    return cast(Any, environment).getattr(obj, key)


class NativeSandboxGetattrTests(unittest.TestCase):
    def test_standard_scalar_format_check_does_not_run_python_body(self) -> None:
        code = SandboxedEnvironment.wrap_str_format.__code__
        for attribute_first in (False, True):
            for value in ('text', 23, 1.5, True, None, b'bytes'):
                with self.subTest(attribute_first=attribute_first, value=value):
                    class Value:
                        public = value

                    def run(native: bool, attribute_first: bool = attribute_first) -> tuple[object, int]:
                        calls = 0

                        def profile(frame: FrameType, event: str, argument: object) -> None:
                            nonlocal calls
                            if event == 'call' and frame.f_code is code:
                                calls += 1

                        environment = SandboxedEnvironment()
                        previous = sys.getprofile()
                        try:
                            sys.setprofile(profile)
                            if native:
                                result = docxtpl_native._extension().python_lookup(
                                    environment, Value(), 'public', attribute_first)
                            elif attribute_first:
                                result = environment.getattr(Value(), 'public')
                            else:
                                result = environment.getitem(Value(), 'public')
                        finally:
                            sys.setprofile(previous)
                        return result, calls

                    expected, reference_calls = run(False)
                    actual, candidate_calls = run(True)
                    self.assertIs(expected, value)
                    self.assertIs(actual, value)
                    self.assertEqual(reference_calls, 1)
                    self.assertEqual(candidate_calls, 0)

    def test_scalar_format_check_preserves_changed_globals_and_overrides(self) -> None:
        for mode in ('isinstance', 'types', 'override'):
            with self.subTest(mode=mode):
                def run(native: bool, mode: str = mode) -> tuple[object, list[str]]:
                    events: list[str] = []

                    class Value:
                        public = 'visible'

                    class Environment(SandboxedEnvironment):
                        def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
                            return True

                    class FormatOverride(Environment):
                        def wrap_str_format(self, value: object) -> Any:
                            events.append('override')
                            return super().wrap_str_format(value)

                    class Types:
                        @property
                        def MethodType(self) -> type:
                            events.append('MethodType')
                            return type(lambda: None)

                        @property
                        def BuiltinMethodType(self) -> type:
                            events.append('BuiltinMethodType')
                            return type(len)

                    def instance(value: object, kind: object) -> bool:
                        events.append('isinstance')
                        return builtins.isinstance(value, cast(Any, kind))

                    replacements: dict[str, object] = {}
                    if mode == 'isinstance':
                        replacements['isinstance'] = instance
                    elif mode == 'types':
                        replacements['types'] = Types()
                    environment = FormatOverride() if mode == 'override' else Environment()
                    with patch.dict(sandbox.__dict__, replacements):
                        result = lookup(native, environment, Value(), 'public')
                    return result, events

                expected = run(False)
                self.assertTrue(expected[1])
                self.assertEqual(run(True), expected)

    def test_string_format_methods_keep_sandbox_wrapper(self) -> None:
        for name, arguments in (('format', ({'obj': {'public': 'ok'}},)),
                                ('format_map', ({'obj': {'public': 'ok'}},))):
            with self.subTest(name=name):
                environment = SandboxedEnvironment()
                source = '{obj[public]}'
                expected = environment.getattr(source, name)
                actual = docxtpl_native._extension().python_lookup(environment, source, name, True)
                if name == 'format':
                    self.assertEqual(actual(**arguments[0]), expected(**arguments[0]))
                else:
                    self.assertEqual(actual(*arguments), expected(*arguments))

    def test_captured_custom_isinstance_is_not_treated_as_builtin(self) -> None:
        from docxtpl_native import bridge

        def run(native: bool) -> tuple[object, list[str]]:
            events: list[str] = []

            class Value:
                public = 'visible'

            class Environment(SandboxedEnvironment):
                def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
                    return True

            def instance(value: object, kind: object) -> bool:
                events.append('isinstance')
                return builtins.isinstance(value, cast(Any, kind))

            with (patch.dict(sandbox.__dict__, {'isinstance': instance}),
                  patch.object(bridge, '_BUILTIN_ISINSTANCE', instance)):
                result = lookup(native, Environment(), Value(), 'public')
            return result, events

        expected = run(False)
        self.assertEqual(expected[1], ['isinstance'])
        self.assertEqual(run(True), expected)

    def test_public_file_route_and_native_policy_bodies(self) -> None:
        codes = {SandboxedEnvironment.getattr.__code__, SandboxedEnvironment.wrap_str_format.__code__,
                 SandboxedEnvironment.is_safe_attribute.__code__,
                 sandbox.is_internal_attribute.__code__}
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

                    document = engine.DocxTemplate(io.BytesIO(template(
                        "{{ obj.public }}|{{ obj._private|default('blocked', true) }}")))
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
                self.assertIn('getattr', reference_calls)
                self.assertIn('wrap_str_format', reference_calls)
                self.assertIn('is_safe_attribute', reference_calls)
                self.assertIn('is_internal_attribute', reference_calls)
                self.assertEqual(candidate_calls, [])

    def test_lookup_policy_order_and_identity(self) -> None:
        for mode in ('item', 'safe', 'unsafe', 'format', 'missing'):
            with self.subTest(mode=mode):
                def run(native: bool, mode: str = mode) -> tuple[list[tuple[str, object]], bool]:
                    events: list[tuple[str, object]] = []
                    sentinel, wrapped, blocked, missing = object(), object(), object(), object()

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

                        def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
                            events.append(('safe', attr))
                            return mode != 'unsafe'

                        def unsafe_undefined(self, obj: Any, attribute: str) -> Any:
                            events.append(('unsafe', attribute))
                            return blocked

                    environment = Environment()

                    def undefined(*, obj: object, name: object) -> object:
                        events.append(('undefined', name))
                        return missing

                    cast(Any, environment).undefined = undefined
                    key = 'absent' if mode in ('item', 'missing') else 'public'
                    result = lookup(native, environment, Value(), key)
                    expected = {'item': sentinel, 'safe': sentinel, 'unsafe': blocked,
                                'format': wrapped, 'missing': missing}[mode]
                    return events, result is expected

                self.assertEqual(run(True), run(False))

    def test_exception_identity_and_context(self) -> None:
        for phase in ('attribute', 'item', 'format', 'policy', 'unsafe', 'undefined'):
            with self.subTest(phase=phase):
                def run(native: bool, phase: str = phase) -> tuple[bool, bool, list[str]]:
                    events: list[str] = []
                    missing_attribute = AttributeError('missing attribute')
                    failure = ValueError('callback failed')

                    class Value:
                        def __getattribute__(self, name: str) -> Any:
                            if name == 'x':
                                events.append('attribute')
                                if phase == 'attribute':
                                    raise failure
                                if phase in ('item', 'undefined'):
                                    raise missing_attribute
                                return 'value'
                            return object.__getattribute__(self, name)

                        def __getitem__(self, key: object) -> object:
                            events.append('item')
                            if phase == 'item':
                                raise failure
                            raise KeyError(key)

                    class Environment(SandboxedEnvironment):
                        def wrap_str_format(self, value: object) -> Any:
                            events.append('format')
                            if phase == 'format':
                                raise failure
                            return None

                        def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
                            events.append('policy')
                            if phase == 'policy':
                                raise failure
                            return False

                        def unsafe_undefined(self, obj: Any, attribute: str) -> Any:
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
                        return error is failure, error.__context__ is missing_attribute, events
                    raise AssertionError('expected callback failure')

                expected = run(False)
                self.assertTrue(expected[0])
                self.assertEqual(run(True), expected)

    def test_uncaught_item_attribute_error_and_non_string_key(self) -> None:
        def run(native: bool, key: object) -> tuple[str, str, bool]:
            failure = AttributeError('item failure')

            class Value:
                def __getitem__(self, key: object) -> object:
                    raise failure

            try:
                lookup(native, SandboxedEnvironment(), Value(), key)
            except (AttributeError, TypeError) as error:
                return type(error).__name__, str(error), error is failure
            raise AssertionError('expected error')

        for key in ('missing', 123):
            with self.subTest(key=key):
                self.assertEqual(run(True, key), run(False, key))

    def test_custom_getattr_and_globals_remain_live(self) -> None:
        def run(native: bool) -> tuple[object, list[str]]:
            events: list[str] = []
            sentinel = object()

            def custom_getattr(obj: object, name: object) -> object:
                events.append('global-getattr')
                return sentinel

            class Environment(SandboxedEnvironment):
                def wrap_str_format(self, value: object) -> Any:
                    events.append('format')
                    return None

                def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
                    events.append('policy')
                    return True

            with patch.dict(sandbox.__dict__, {'getattr': custom_getattr}):
                result = lookup(native, Environment(), object(), 'x')
            return result is sentinel, events

        self.assertEqual(run(True), run(False))
