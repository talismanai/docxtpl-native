"""Differential tests and execution-boundary audit for the title filter."""

import sys
import unittest
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from types import FrameType

import docxtpl
import docxtpl_native
import jinja2
from jinja2 import filters
from markupsafe import Markup
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate
from test_native_filter_evaluation import render


@contextmanager
def no_python_title() -> Generator[None, None, None]:
    code = filters.do_title.__code__
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
        raise AssertionError('original title body executed: ' + repr(calls))


class NativeTitleFilterTests(unittest.TestCase):
    def test_runtime_and_constant_unicode(self) -> None:
        for value in ('', 'hello-WORLD\tfoo_bar', 'straße ΣΊΓΜΑ', 'a\ud800b', Markup('<b>HEY</b>')):
            for literal in (False, True):
                if literal and isinstance(value, Markup):
                    continue
                expression = repr(value) if literal else 'value'
                suffix = '|wordcount' if value == 'a\ud800b' else ''
                source = '{{ ' + expression + '|title' + suffix + ' }}'
                with self.subTest(value=value, literal=literal):
                    expected = render(source, {'value': value}, False)
                    with no_python_title():
                        actual = render(source, {'value': value}, True)
                    self.assertEqual(actual, expected)

    def test_replaced_pattern_protocols_and_failure(self) -> None:
        original = filters._word_beginning_split_re

        def observe(native: bool, fail: bool) -> tuple[object, list[str]]:
            events: list[str] = []

            class Token:
                def __bool__(self) -> bool:
                    events.append('bool')
                    return True

                def __getitem__(self, key: object) -> str:
                    events.append('index' if key == 0 else 'slice')
                    if fail and key == 0:
                        raise ValueError('index failure')
                    return 'a' if key == 0 else 'BC'

            class Pattern:
                def split(self, value: object) -> list[object]:
                    events.append('split:' + str(value))
                    return [Token(), '', 'WORLD']

            filters.__dict__['_word_beginning_split_re'] = Pattern()
            try:
                try:
                    result = render('{{ value|title }}', {'value': 'hello'}, native)
                except ValueError as error:
                    result = (type(error).__name__, str(error))
                return result, events
            finally:
                filters.__dict__['_word_beginning_split_re'] = original

        for fail in (False, True):
            with self.subTest(fail=fail):
                expected = observe(False, fail)
                with no_python_title():
                    actual = observe(True, fail)
                self.assertEqual(actual, expected)

    def test_complete_document(self) -> None:
        for asynchronous in (False, True):
            environment = jinja2.Environment(enable_async=asynchronous)
            reference = generate(docxtpl, '{{ value|title }}', {'value': 'café-WORLD'}, environment)
            with no_python_title():
                candidate = generate(docxtpl_native, '{{ value|title }}', {'value': 'café-WORLD'}, environment)
            with self.subTest(asynchronous=asynchronous):
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_soft_str_override_and_iterator_failure(self) -> None:
        original_soft_str = filters.soft_str
        original_pattern = filters._word_beginning_split_re

        def observe(native: bool, fail: bool) -> tuple[object, list[str]]:
            events: list[str] = []

            def replacement(value: object) -> str:
                events.append('soft_str:' + str(value))
                return 'replacement'

            class Pattern:
                def split(self, value: object) -> Iterator[str]:
                    events.append('split:' + str(value))
                    yield 'aBC'
                    if fail:
                        raise ValueError('iteration failure')
                    yield 'HELLO'

            filters.__dict__['soft_str'] = replacement
            filters.__dict__['_word_beginning_split_re'] = Pattern()
            try:
                try:
                    result = render('{{ value|title }}', {'value': 'input'}, native)
                except ValueError as error:
                    result = (type(error).__name__, str(error))
                return result, events
            finally:
                filters.__dict__['soft_str'] = original_soft_str
                filters.__dict__['_word_beginning_split_re'] = original_pattern

        for fail in (False, True):
            with self.subTest(fail=fail):
                expected = observe(False, fail)
                with no_python_title():
                    actual = observe(True, fail)
                self.assertEqual(actual, expected)
