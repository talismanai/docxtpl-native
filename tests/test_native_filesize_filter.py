"""Differential tests and execution-boundary audit for filesizeformat."""

import random
import sys
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from types import FrameType
from typing import Any

import docxtpl
import docxtpl_native
import jinja2
from jinja2 import filters
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate
from test_native_filter_evaluation import render


@contextmanager
def no_python_filesize() -> Generator[None, None, None]:
    code = filters.do_filesizeformat.__code__
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
        raise AssertionError('original filesizeformat executed: ' + repr(calls))


def observe(value: Any, binary: Any, native: bool) -> tuple[str, str, str]:
    try:
        return ('result', render('{{ value|filesizeformat(binary) }}',
                                 {'value': value, 'binary': binary}, native), '')
    except (TypeError, ValueError, OverflowError, UnboundLocalError) as error:
        return ('error', type(error).__name__, str(error))


class NativeFilesizeFilterTests(unittest.TestCase):
    def test_numeric_grid(self) -> None:
        randomizer = random.Random(7481)
        values = [randomizer.uniform(-2.0, 2.0) * 10 ** exponent
                  for exponent in range(-4, 31) for _ in range(3)]
        for value in values:
            for binary in (False, True):
                with self.subTest(value=value, binary=binary):
                    expected = observe(value, binary, False)
                    with no_python_filesize():
                        actual = observe(value, binary, True)
                    self.assertEqual(actual, expected)

    def test_boundaries_and_errors(self) -> None:
        values = [0, 1, -1, 999, 1000, 1023, 1024, 999999, 1e24, 1e30,
                  1.25, '1000', 'invalid', float('inf'), float('-inf'), float('nan')]
        for value in values:
            for binary in (False, True):
                with self.subTest(value=value, binary=binary):
                    expected = observe(value, binary, False)
                    with no_python_filesize():
                        actual = observe(value, binary, True)
                    self.assertEqual(actual, expected)

    def test_truth_and_numeric_protocol_order(self) -> None:
        def run(native: bool) -> tuple[tuple[str, str, str], list[object]]:
            events: list[object] = []

            class Number:
                def __float__(self) -> float:
                    events.append('float')
                    return 12345.0

            class Binary:
                def __bool__(self) -> bool:
                    events.append('bool')
                    return True

            return observe(Number(), Binary(), native), events

        expected = run(False)
        with no_python_filesize():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_overridden_globals(self) -> None:
        def run(native: bool) -> tuple[tuple[str, str, str], list[object]]:
            events: list[object] = []

            def replacement(value: object) -> float:
                events.append(('float', value))
                return 42.0

            def integer(value: object) -> str:
                events.append(('int', value))
                return 'override'

            filters.__dict__['float'] = replacement
            filters.__dict__['int'] = integer
            try:
                return observe('x', False, native), events
            finally:
                del filters.__dict__['float']
                del filters.__dict__['int']

        expected = run(False)
        with no_python_filesize():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_constant_and_numeric_override(self) -> None:
        source = '{{ "1024"|filesizeformat(true) }}'
        expected = render(source, {}, False)
        with no_python_filesize():
            actual = render(source, {}, True)
        self.assertEqual(actual, expected)

        def run(native: bool) -> tuple[tuple[str, str, str], list[object]]:
            events: list[object] = []

            class Numeric:
                def __eq__(self, other: object) -> bool:
                    events.append(('eq', other))
                    return False

                def __lt__(self, other: object) -> bool:
                    events.append(('lt', other))
                    return other != 1000

                def __rmul__(self, other: object) -> 'Numeric':
                    events.append(('rmul', other))
                    return self

                def __truediv__(self, other: object) -> 'Numeric':
                    events.append(('div', other))
                    return self

                def __format__(self, spec: str) -> str:
                    events.append(('format', spec))
                    return 'custom'

            def replacement(value: object) -> Numeric:
                events.append(('float', value))
                return Numeric()

            filters.__dict__['float'] = replacement
            try:
                return observe('source', False, native), events
            finally:
                del filters.__dict__['float']

        expected_trace = run(False)
        with no_python_filesize():
            actual_trace = run(True)
        self.assertEqual(actual_trace, expected_trace)

    def test_complete_document(self) -> None:
        for asynchronous in (False, True):
            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{{ value|filesizeformat(true) }}'
            expected = generate(docxtpl, source, {'value': 123456}, environment)
            with no_python_filesize():
                actual = generate(docxtpl_native, source, {'value': 123456}, environment)
            with self.subTest(asynchronous=asynchronous):
                self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_enumerate_override_and_empty_iterator(self) -> None:
        for empty in (False, True):
            def run(native: bool, empty: bool = empty) -> tuple[tuple[str, str, str], list[object]]:
                events: list[object] = []

                def replacement(prefixes: list[str]) -> list[tuple[int, str]]:
                    events.append(tuple(prefixes))
                    return [] if empty else [(0, 'custom')]

                filters.__dict__['enumerate'] = replacement
                try:
                    return observe(2000, False, native), events
                finally:
                    del filters.__dict__['enumerate']

            with self.subTest(empty=empty):
                expected = run(False)
                with no_python_filesize():
                    actual = run(True)
                self.assertEqual(actual, expected)
