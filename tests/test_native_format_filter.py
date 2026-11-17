"""Differential and native-boundary checks for the Jinja format filter."""

import sys
import unittest
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from types import FrameType

import docxtpl
import docxtpl_native
import jinja2
from jinja2 import filters
from jinja2.exceptions import FilterArgumentError
from markupsafe import Markup
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate
from test_native_filter_evaluation import render


@contextmanager
def no_python_format() -> Generator[None, None, None]:
    code = filters.do_format.__code__
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
        raise AssertionError('original format body executed: ' + repr(calls))


def outcome(source: str, native: bool, values: Mapping[str, object]) -> tuple[str, str, str]:
    try:
        return ('result', render(source, values, native), '')
    except (FilterArgumentError, TypeError, ValueError, KeyError) as error:
        return ('error', type(error).__name__, str(error))


class NativeFormatFilterTests(unittest.TestCase):
    def test_values_and_errors(self) -> None:
        for source, values in [
            ('{{ pattern|format("abc", 12) }}', {'pattern': '%s:%04d'}),
            ('{{ "%(name)s:%(number)04d"|format(name="abc", number=12) }}', {}),
            ('{{ pattern|format(name="abc") }}', {'pattern': '%(name)s'}),
            ('{{ pattern|format() }}', {'pattern': 'plain'}),
            ('{{ pattern|format(1, name="x") }}', {'pattern': '%s'}),
            ('{{ pattern|format(value="duplicate") }}', {'pattern': '%s'}),
            ('{{ pattern|format() }}', {'pattern': '%d'}),
            ('{{ pattern|format(missing="x") }}', {'pattern': '%(other)s'}),
            ('{{ pattern|format(**kwargs) }}', {'pattern': '%(name)s', 'kwargs': {'name': 'X'}}),
            ('{{ pattern|format(*items, **kwargs) }}', {'pattern': '%s', 'items': [1], 'kwargs': {'name': 'X'}}),
        ]:
            with self.subTest(source=source):
                expected = outcome(source, False, values)
                with no_python_format():
                    actual = outcome(source, True, values)
                self.assertEqual(actual, expected)

    def test_soft_str_override_and_callbacks(self) -> None:
        original = filters.soft_str

        def observe(native: bool) -> tuple[tuple[str, str, str], list[str]]:
            events: list[str] = []

            class Value:
                def __str__(self) -> str:
                    events.append('str')
                    return 'item'

            def replacement(value: object) -> str:
                events.append('soft:' + str(value))
                return '[%s]'

            filters.__dict__['soft_str'] = replacement
            try:
                return outcome('{{ pattern|format(value) }}', native,
                               {'pattern': 'input', 'value': Value()}), events
            finally:
                filters.__dict__['soft_str'] = original

        expected = observe(False)
        with no_python_format():
            actual = observe(True)
        self.assertEqual(actual, expected)

    def test_complete_document(self) -> None:
        source = '{{ "%s:%03d"|format(name, number) }}'
        values = {'name': 'Café', 'number': 5}
        reference = generate(docxtpl, source, values)
        with no_python_format():
            candidate = generate(docxtpl_native, source, values)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_markup_and_autoescape(self) -> None:
        for autoescape in (False, True):
            environment = jinja2.Environment(autoescape=autoescape)
            source = '{{ pattern|format(value) }}'
            values = {'pattern': Markup('<b>%s</b>'), 'value': '<x>'}
            expected = render(source, values, False, environment)
            with no_python_format():
                actual = render(source, values, True, environment)
            self.assertEqual(actual, expected)

    def test_dynamic_keyword_subclass_protocol(self) -> None:
        def observe(native: bool, key_text: str) -> tuple[tuple[str, str, str], list[str]]:
            events: list[str] = []

            class Key(str):
                def __hash__(self) -> int:
                    events.append('hash')
                    return super().__hash__()

                def __eq__(self, other: object) -> bool:
                    events.append('eq:' + str(other))
                    return super().__eq__(other)

            key = Key(key_text)
            kwargs = {key: 'X'}
            events.clear()
            result = outcome('{{ pattern|format(**kwargs) }}', native,
                             {'pattern': '%(name)s', 'kwargs': kwargs})
            return result, events

        for key_text in ('name', 'value'):
            with self.subTest(key=key_text):
                expected = observe(False, key_text)
                with no_python_format():
                    actual = observe(True, key_text)
                self.assertEqual(actual, expected)
