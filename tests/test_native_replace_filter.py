"""Differential tests and execution-boundary audit for the replace filter."""

import html
import io
import re
import subprocess
import sys
import unittest
import zipfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from types import FrameType
from typing import Any, Self
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
import markupsafe
from jinja2 import filters
from markupsafe import Markup
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate
from test_native_filter_evaluation import render


def document_text(docx: bytes) -> str:
    """The visible text of the main document."""
    with zipfile.ZipFile(io.BytesIO(docx)) as package:
        xml = package.read('word/document.xml').decode()
    return html.unescape(''.join(re.findall(r'<w:t(?:\s[^>]*)?>([^<]*)</w:t>', xml)))


@contextmanager
def no_python_replace_filter() -> Generator[None, None, None]:
    code = filters.do_replace.__code__
    previous = sys.getprofile()
    calls: list[str] = []

    def profile(frame: FrameType, event: str, argument: object) -> None:
        if event == 'call' and frame.f_code is code:
            calls.append(frame.f_code.co_name)

    try:
        sys.setprofile(profile)
        yield
    finally:
        sys.setprofile(previous)
    if calls:
        raise AssertionError('standard filter body executed: ' + repr(calls))


@contextmanager
def no_python_markup_replace() -> Generator[None, None, None]:
    codes = {filters.do_replace.__code__, Markup.replace.__code__,
             Markup.escape.__func__.__code__, markupsafe.escape.__code__}
    previous = sys.getprofile()
    calls: list[str] = []

    def profile(frame: FrameType, event: str, argument: object) -> None:
        if event == 'call' and frame.f_code in codes:
            calls.append(frame.f_code.co_qualname)

    try:
        sys.setprofile(profile)
        yield
    finally:
        sys.setprofile(previous)
    if calls:
        raise AssertionError('standard Markup bodies executed: ' + repr(calls))


class NativeReplaceFilterTests(unittest.TestCase):
    def test_missing_soft_str_preserves_name_error_attribute(self) -> None:
        def run(native: bool) -> tuple[str, str, str | None]:
            with patch.dict(filters.__dict__):
                del filters.__dict__['soft_str']
                try:
                    render("{{ value|replace('a','b') }}", {'value': 'a'}, native,
                           jinja2.Environment(autoescape=True))
                except NameError as error:
                    return type(error).__name__, str(error), error.name
            raise AssertionError('expected missing soft_str')

        expected = run(False)
        self.assertEqual(expected, ('NameError', "name 'soft_str' is not defined", 'soft_str'))
        with no_python_replace_filter():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_temporary_escape_callback_is_released_before_replace(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Escape:
                def __call__(self, value: object) -> Markup:
                    events.append('escape:call')
                    return Markup('x')

                def __del__(self) -> None:
                    events.append('escape:drop')

            class Value(Markup):
                # Exercise a dynamic descriptor with a caller-visible lifetime.
                @property
                def escape(self) -> Escape:  # ty: ignore[invalid-property-type-override]
                    events.append('escape:lookup')
                    return Escape()

            class Count:
                def __index__(self) -> int:
                    events.append('count:index')
                    return 1

            output = render("{{ value|replace('a',new,count) }}",
                            {'value': Value('aba'), 'new': '<x>', 'count': Count()},
                            native, jinja2.Environment(autoescape=True))
            return output, events

        expected = run(False)
        with no_python_markup_replace():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_super_temporary_lifetime_when_replace_raises(self) -> None:
        def run(native: bool) -> tuple[tuple[str, str, bool], list[str]]:
            events: list[str] = []
            marker = ValueError('replace failed')

            class Parent:
                def __getattribute__(self, name: str) -> object:
                    if name == '__new__':
                        return str.__new__
                    return object.__getattribute__(self, name)

                def replace(self, old: object, new: object, count: object) -> str:
                    events.append('parent:replace')
                    raise marker

                def __del__(self) -> None:
                    events.append('parent:drop')

            def custom_super() -> Parent:
                events.append('super')
                return Parent()

            value = Markup('a')
            with patch.dict(markupsafe.__dict__, {'super': custom_super}):
                try:
                    render("{{ value|replace('a','b') }}", {'value': value}, native,
                           jinja2.Environment(autoescape=True))
                except ValueError as error:
                    result = (type(error).__name__, str(error), error is marker)
                    observed = events.copy()
                else:
                    raise AssertionError('expected ValueError')
            return result, observed

        expected = run(False)
        with no_python_markup_replace():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_temporary_super_is_released_before_result_construction(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Value(Markup):
                def __new__(cls, text: str = '') -> Self:
                    events.append('value:new')
                    return super().__new__(cls, text)

            class Parent:
                def __getattribute__(self, name: str) -> object:
                    if name == '__new__':
                        return str.__new__
                    return object.__getattribute__(self, name)

                def replace(self, old: object, new: object, count: object) -> str:
                    events.append('parent:replace')
                    return 'result'

                def __del__(self) -> None:
                    events.append('parent:drop')

            def custom_super() -> Parent:
                events.append('super')
                return Parent()

            value = Value('a')
            events.clear()
            with patch.dict(markupsafe.__dict__, {'super': custom_super}):
                output = render("{{ value|replace('a','b') }}", {'value': value}, native,
                                jinja2.Environment(autoescape=True))
            return output, events

        expected = run(False)
        with no_python_markup_replace():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_markup_result_type_reaches_callback(self) -> None:
        def run(native: bool, subclass: bool) -> tuple[str, list[tuple[str, bool]]]:
            events: list[tuple[str, bool]] = []

            class Value(Markup):
                pass

            def observe(value: object) -> str:
                events.append((type(value).__name__, isinstance(value, Value)))
                return str(value)

            value = Value('aba') if subclass else Markup('aba')
            output = render("{{ observe(value|replace('a','<x>',1)) }}",
                            {'value': value, 'observe': observe}, native,
                            jinja2.Environment(autoescape=True))
            return output, events

        for subclass in (False, True):
            with self.subTest(subclass=subclass):
                expected = run(False, subclass)
                with no_python_markup_replace():
                    actual = run(True, subclass)
                self.assertEqual(actual, expected)

    def test_callable_escape_replaced_before_import_preserves_native_methods(self) -> None:
        script = '''
import sys
import jinja2
import markupsafe
import docxtpl_native
from docxtpl_native.bridge import BridgeConfig

events: list[str] = []
class Escape:
    def __call__(self, value: object) -> markupsafe.Markup:
        events.append("escape:" + str(value))
        return markupsafe.Markup("X")
markupsafe.escape = Escape()
source = "{{ value|replace('a','b') }}"
context = {"value": markupsafe.Markup("a")}
environment = jinja2.Environment(autoescape=True)
expected = environment.from_string(source).render(context)
expected_events = events.copy()
events.clear()
session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
codes = {markupsafe.Markup.replace.__code__, markupsafe.Markup.escape.__func__.__code__}
calls: list[str] = []
def profile(frame: object, event: str, argument: object) -> None:
    if event == "call" and frame.f_code in codes:
        calls.append(frame.f_code.co_name)
sys.setprofile(profile)
try:
    actual = session.render("case", source)
finally:
    sys.setprofile(None)
assert (actual, events) == (expected, expected_events), (actual, events, expected, expected_events)
assert calls == [], calls
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_replaced_method_type_does_not_probe_custom_callable(self) -> None:
        script = '''
import types
import jinja2
import jinja2.filters
import docxtpl_native
from docxtpl_native.bridge import BridgeConfig

class CustomCallable:
    def __getattribute__(self, name: str) -> object:
        if name in ("__func__", "__self__"):
            raise RuntimeError("unexpected method probe")
        return object.__getattribute__(self, name)
    def __call__(self, old: object, new: object, count: object) -> str:
        return "ok"
class Receiver:
    @property
    def replace(self) -> CustomCallable:
        return CustomCallable()
value = object()
def soft(item: object) -> object:
    return Receiver() if item is value else "x"
jinja2.filters.soft_str = soft
types.MethodType = CustomCallable
environment = jinja2.Environment(autoescape=True)
context = {"value": value, "old": "a", "new": "b"}
session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
assert session.render("case", "{{ value|replace(old,new) }}") == "ok"
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_replaced_super_with_empty_class_cell(self) -> None:
        def run(native: bool) -> tuple[object, list[str]]:
            events: list[str] = []
            closure = Markup.replace.__closure__
            assert closure is not None
            cell: Any = closure[0]
            original = cell.cell_contents
            state = {'emptied': False}

            class Value(Markup):
                def __getattribute__(self, name: str) -> object:
                    if name == '__class__' and not state['emptied']:
                        events.append('class')
                        state['emptied'] = True
                        del cell.cell_contents
                    return super().__getattribute__(name)

            class Parent:
                def __getattribute__(self, name: str) -> object:
                    if name == '__new__':
                        return str.__new__
                    return object.__getattribute__(self, name)

                def replace(self, old: object, new: object, count: object) -> str:
                    events.append('replace')
                    return 'result'

            def custom_super() -> Parent:
                events.append('super')
                return Parent()

            try:
                with patch.dict(markupsafe.__dict__, {'super': custom_super}):
                    try:
                        result: object = render("{{ value|replace('a','b') }}", {'value': Value('a')},
                                                native, jinja2.Environment(autoescape=True))
                    except (RuntimeError, NameError, TypeError) as error:
                        result = (type(error).__name__, str(error))
                return result, events
            finally:
                cell.cell_contents = original

        expected = run(False)
        with no_python_markup_replace():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_markup_restored_after_extension_initialization(self) -> None:
        script = '''
import sys
import jinja2
import markupsafe
import docxtpl_native
from contextlib import ExitStack
from unittest.mock import patch
from test_native_filter_evaluation import render

original = markupsafe.Markup
codes = {original.replace.__code__, original.escape.__func__.__code__, markupsafe.escape.__code__}
events: list[str] = []
def markup(value: object) -> object:
    events.append("replacement called")
    return original(value)
with ExitStack() as stack:
    stack.enter_context(patch.dict(markupsafe.__dict__, {"Markup": markup}))
    if MODE == "both":
        stack.enter_context(patch.dict(jinja2.filters.__dict__, {"Markup": markup}))
    docxtpl_native._extension()
assert events == [], events

def profile(frame: object, event: str, arg: object) -> None:
    if event == "call" and frame.f_code in codes:
        events.append(frame.f_code.co_qualname)

source = "{{ value|replace('a', 'x') }}"
context = {"value": original("<a>")}
if MODE == "both":
    callback_events: list[str] = []
    def replacement(self: object, old: object, new: object, count: object = -1) -> object:
        callback_events.append("replace")
        return original("custom")
    environment = jinja2.Environment(autoescape=True)
    with patch.object(original, "replace", replacement):
        expected = render(source, context, False, environment)
        expected_events = callback_events.copy()
        callback_events.clear()
        actual = render(source, context, True, environment)
    assert (actual, callback_events) == (expected, expected_events)
for asynchronous in (False, True):
    environment = jinja2.Environment(autoescape=True, enable_async=asynchronous)
    expected = render(source, context, False, environment)
    sys.setprofile(profile)
    try:
        actual = render(source, context, True, environment)
    finally:
        sys.setprofile(None)
    assert actual == expected, (actual, expected)
    assert events == [], events
'''
        for mode in ('markupsafe', 'both'):
            with self.subTest(mode=mode):
                result = subprocess.run([sys.executable, '-c', 'MODE = ' + repr(mode) + '\n' + script],
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_import_does_not_probe_replaced_markup(self) -> None:
        script = '''
import jinja2
import markupsafe
import builtins
import docxtpl_native
import docxtpl_native.bridge

events: list[str] = []
class Descriptor:
    def __getattribute__(self, name: str) -> object:
        if name == "__func__":
            events.append(name)
            raise RuntimeError("unexpected function probe")
        return object.__getattribute__(self, name)
    def __get__(self, instance: object, owner: object) -> object:
        events.append("descriptor")
        raise RuntimeError("unexpected descriptor")
class Placeholder:
    def __getattribute__(self, name: str) -> object:
        if name in ("replace", "escape"):
            events.append(name)
            raise RuntimeError("unexpected attribute")
        return object.__getattribute__(self, name)

if MODE == "instance":
    markupsafe.Markup = Placeholder()
else:
    module_name = chr(0xd800) if MODE == "module_surrogate" else "markupsafe"
    markupsafe.Markup = type("Markup", (), {"__module__": module_name,
                                            "replace": Descriptor(), "escape": Descriptor()})
    if MODE == "classmethod":
        builtins.classmethod = Descriptor
docxtpl_native._extension()
assert events == [], events
'''
        for mode in ('instance', 'descriptor', 'classmethod', 'module_surrogate'):
            with self.subTest(mode=mode):
                result = subprocess.run([sys.executable, '-c', 'MODE = ' + repr(mode) + '\n' + script],
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_markup_document_matches_without_python_replace_bodies(self) -> None:
        source = "{{ value|replace('a',new,1) }}"
        context = {'value': Markup('<a>a</a>'), 'new': '<x>'}
        for asynchronous in (False, True):
            environment = jinja2.Environment(autoescape=True, enable_async=asynchronous)
            reference = generate(docxtpl, source, context, environment)
            with no_python_markup_replace():
                candidate = generate(docxtpl_native, source, context, environment)
            with self.subTest(asynchronous=asynchronous):
                # The rendered text `<&lt;x&gt;>a</a>` is not well-formed. The
                # reference's recover mode keeps only `x>a`; the native engine
                # keeps the text (`<<x>>a`) on purpose and drops the stray end tag.
                self.assertEqual(document_text(reference), 'x>a')
                self.assertEqual(document_text(candidate), '<<x>>a')

    def test_replaced_markup_replace_remains_a_callback(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def replacement(receiver: Markup, old: object, new: object, count: object) -> Markup:
                events.append('replace:' + str(receiver) + ':' + str(count))
                return Markup('<custom>')

            with patch.object(Markup, 'replace', replacement):
                output = render("{{ value|replace('a','b') }}", {'value': Markup('a')},
                                native, jinja2.Environment(autoescape=True))
            return output, events

        expected = run(False)
        with no_python_markup_replace():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_markup_class_lookup_precedes_live_closure_read(self) -> None:
        def run(native: bool, empty: bool) -> tuple[object, list[str]]:
            events: list[str] = []
            closure = Markup.replace.__closure__
            assert closure is not None
            cell: Any = closure[0]
            original = cell.cell_contents

            class Value(Markup):
                def __getattribute__(self, name: str) -> object:
                    if name == '__class__':
                        events.append('class')
                        if empty:
                            del cell.cell_contents
                        else:
                            cell.cell_contents = int
                    return super().__getattribute__(name)

            try:
                try:
                    result: object = render("{{ value|replace('a','b') }}", {'value': Value('a')},
                                            native, jinja2.Environment(autoescape=True))
                except (TypeError, NameError, RuntimeError) as error:
                    result = (type(error).__name__, str(error))
                return result, events
            finally:
                cell.cell_contents = original

        for empty in (False, True):
            with self.subTest(empty=empty):
                expected = run(False, empty)
                with no_python_markup_replace():
                    actual = run(True, empty)
                self.assertEqual(actual, expected)

    def test_markup_replace_and_escape_execute_natively(self) -> None:
        for asynchronous in (False, True):
            environment = jinja2.Environment(autoescape=True, enable_async=asynchronous)
            context = {'value': Markup('<a>a</a>'), 'new': '<x>'}
            source = "{{ value|replace('a',new,1) }}"
            expected = render(source, context, False, environment)
            with no_python_markup_replace():
                actual = render(source, context, True, environment)
            self.assertEqual(actual, expected)

    def test_markup_subclass_and_escape_override(self) -> None:
        def run(native: bool, override: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Value(Markup):
                def __getattribute__(self, name: str) -> object:
                    if name in ('__class__', 'escape', 'replace'):
                        events.append('get:' + name)
                    return super().__getattribute__(name)

            class WithEscape(Value):
                @classmethod
                def escape(cls, value: object) -> 'WithEscape':
                    events.append('custom escape:' + str(value))
                    return cls('<custom>')

            value = WithEscape('aba') if override else Value('aba')
            output = render("{{ value|replace('a',new,1) }}", {'value': value, 'new': '<x>'},
                            native, jinja2.Environment(autoescape=True))
            return output, events

        for override in (False, True):
            with self.subTest(override=override):
                expected = run(False, override)
                with no_python_markup_replace():
                    actual = run(True, override)
                self.assertEqual(actual, expected)

    def test_replaced_super_and_module_escape_are_callbacks(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class SuperResult:
                def __getattribute__(self, name: str) -> object:
                    if name == '__new__':
                        return str.__new__
                    return object.__getattribute__(self, name)

                def replace(self, old: object, new: object, count: object) -> str:
                    events.append('super replace:' + str(count))
                    return 'custom-base'

            def custom_super() -> SuperResult:
                events.append('super')
                return SuperResult()

            def custom_escape(value: object) -> Markup:
                events.append('escape:' + str(value))
                return Markup('escaped')

            environment = jinja2.Environment(autoescape=True)
            with patch.dict(markupsafe.__dict__, {'super': custom_super, 'escape': custom_escape}):
                output = render("{{ value|replace('a',new,1) }}", {'value': Markup('aba'), 'new': '<x>'},
                                native, environment)
            return output, events

        expected = run(False)
        with no_python_markup_replace():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_keywords_count_protocol_and_errors(self) -> None:
        for source, context in (
            ("{{ value|replace(old='a',new='x') }}", {'value': 'aba'}),
            ("{{ value|replace('a','x', count=1) }}", {'value': 'aba'}),
            ("{{ value|replace('a','x', count='bad') }}", {'value': 'aba'}),
            ("{{ value|replace('a') }}", {'value': 'aba'}),
            ("{{ value|replace('a','x', extra=1) }}", {'value': 'aba'}),
        ):
            def run(native: bool, template: str, values: Mapping[str, object]) -> tuple[str, str, str]:
                try:
                    return ('result', render(template, values, native), '')
                except (TypeError, ValueError) as error:
                    return ('error', type(error).__name__, str(error))

            with self.subTest(source=source):
                expected = run(False, source, context)
                with no_python_replace_filter():
                    actual = run(True, source, context)
                self.assertEqual(actual, expected)

        def run_index(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Count:
                def __index__(self) -> int:
                    events.append('index')
                    return 1

            value = render("{{ value|replace('a','x',count) }}", {'value': 'aba', 'count': Count()}, native)
            return value, events

        expected_index = run_index(False)
        with no_python_replace_filter():
            actual_index = run_index(True)
        self.assertEqual(actual_index, expected_index)

    def test_values_constants_and_complete_documents(self) -> None:
        for asynchronous in (False, True):
            for autoescape in (False, True):
                for value, old, new, count in (
                    ('a<a', 'a', '<x>', None),
                    (Markup('<a>a</a>'), 'a', '<b>', 1),
                    ('a&a', Markup('a'), Markup('<x>'), -1),
                    ('a&a', 'a', Markup('<x>'), 0),
                ):
                    environment = jinja2.Environment(enable_async=asynchronous, autoescape=autoescape)
                    context = {'value': value, 'old': old, 'new': new, 'count': count}
                    source = '{{ value|replace(old,new,count) }}'
                    with self.subTest(asynchronous=asynchronous, autoescape=autoescape, context=context):
                        expected = render(source, context, False, environment)
                        with no_python_replace_filter():
                            actual = render(source, context, True, environment)
                        self.assertEqual(actual, expected)
                environment = jinja2.Environment(enable_async=asynchronous, autoescape=autoescape)
                source = "{{ 'aba'|replace('a','<x>',1) }}"
                expected = render(source, {}, False, environment)
                with no_python_replace_filter():
                    actual = render(source, {}, True, environment)
                self.assertEqual(actual, expected)

            environment = jinja2.Environment(enable_async=asynchronous, autoescape=True)
            source = "{{ value|replace('a','<x>') }}"
            reference = generate(docxtpl, source, {'value': 'a&a'}, environment)
            with no_python_replace_filter():
                candidate = generate(docxtpl_native, source, {'value': 'a&a'}, environment)
            self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_protocol_order_and_exception_identity(self) -> None:
        def run(native: bool, autoescape: bool, fail: bool) -> tuple[object, list[str]]:
            events: list[str] = []
            marker = ValueError('conversion failed')

            class Item:
                def __init__(self, name: str, text: str) -> None:
                    self.name = name
                    self.text = text

                def __str__(self) -> str:
                    events.append('str:' + self.name)
                    if self.name == 'new' and fail:
                        raise marker
                    return self.text

                def __getattribute__(self, name: str) -> object:
                    if name == '__html__':
                        events.append('html:' + object.__getattribute__(self, 'name'))
                        raise AttributeError(name)
                    return object.__getattribute__(self, name)

            context = {'value': Item('value', 'aba'), 'old': Item('old', 'a'), 'new': Item('new', '<x>')}
            environment = jinja2.Environment(autoescape=autoescape)
            try:
                result: object = render('{{ value|replace(old,new) }}', context, native, environment)
            except ValueError as error:
                result = (type(error).__name__, str(error), error is marker)
            return result, events

        for autoescape in (False, True):
            for fail in (False, True):
                with self.subTest(autoescape=autoescape, fail=fail):
                    expected = run(False, autoescape, fail)
                    with no_python_replace_filter():
                        actual = run(True, autoescape, fail)
                    self.assertEqual(actual, expected)

    def test_live_escape_and_soft_str_overrides(self) -> None:
        def run(native: bool, use_escape: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def escape(value: object) -> str:
                events.append('escape:' + str(value))
                return 'aba'

            def soft(value: object) -> str:
                events.append('soft:' + str(value))
                return str(value)

            old = Markup('a') if use_escape else 'a'
            environment = jinja2.Environment(autoescape=True)
            with patch.dict(filters.__dict__, {'escape': escape, 'soft_str': soft}):
                result = render('{{ value|replace(old,new) }}',
                                {'value': 'aba', 'old': old, 'new': 'x'}, native, environment)
            return result, events

        for use_escape in (False, True):
            with self.subTest(use_escape=use_escape):
                expected = run(False, use_escape)
                with no_python_replace_filter():
                    actual = run(True, use_escape)
                self.assertEqual(actual, expected)

    def test_replace_method_is_captured_before_argument_conversion(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            state = {'method': 'original'}
            value = object()
            old = object()
            new = object()

            class Receiver:
                @property
                def replace(self) -> object:
                    captured = state['method']
                    events.append('lookup:' + captured)

                    def invoke(old_value: object, new_value: object, count: object) -> str:
                        events.append('call:' + captured + ':' + str(count))
                        return captured

                    return invoke

            def soft(item: object) -> object:
                if item is value:
                    events.append('soft:value')
                    return Receiver()
                if item is old:
                    events.append('soft:old')
                    state['method'] = 'changed'
                    return 'a'
                events.append('soft:new')
                return 'b'

            with patch.dict(filters.__dict__, {'soft_str': soft}):
                output = render('{{ value|replace(old,new) }}', {'value': value, 'old': old, 'new': new},
                                native, jinja2.Environment(autoescape=True))
            return output, events

        expected = run(False)
        with no_python_replace_filter():
            actual = run(True)
        self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
