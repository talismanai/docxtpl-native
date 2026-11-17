"""Differential behavior and execution-boundary audit for truncate."""

import subprocess
import sys
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from types import FrameType
from typing import Any

import docxtpl
import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import filters
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate
from test_native_filter_evaluation import render


@contextmanager
def no_python_truncate() -> Generator[None, None, None]:
    code = filters.do_truncate.__code__
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
        raise AssertionError('original truncate body executed: ' + repr(calls))


def observe(source: str, values: dict[str, Any], native: bool,
            environment: jinja2.Environment | None = None) -> tuple[str, str, str]:
    try:
        return ('result', render(source, values, native, environment), '')
    except (AssertionError, TypeError, ValueError, KeyError) as error:
        return ('error', type(error).__name__, str(error))


class NativeTruncateFilterTests(unittest.TestCase):
    def test_constants_runtime_keywords_and_errors(self) -> None:
        for expression, values in [
            ('value|truncate(9)', {'value': 'foo bar baz qux'}),
            ('value|truncate(9, true)', {'value': 'foo bar baz qux'}),
            ('value|truncate(11, false, "...", 0)', {'value': 'foo bar baz qux'}),
            ('"foo bar baz qux"|truncate(9, true, "!", 0)', {}),
            ('value|truncate(2, end="...")', {'value': 'text'}),
            ('value|truncate(9, leeway=-1)', {'value': 'text'}),
            ('value|truncate(length=8, end="..", leeway=0)', {'value': 'abcdefghi'}),
            ('value|truncate(8, unexpected=true)', {'value': 'abcdefghi'}),
        ]:
            source = '{{ ' + expression + ' }}'
            with self.subTest(expression=expression):
                expected = observe(source, values, False)
                with no_python_truncate():
                    actual = observe(source, values, True)
                self.assertEqual(actual, expected)

    def test_policy_and_user_protocol_order(self) -> None:
        def run(native: bool, killwords: bool) -> tuple[tuple[str, str, str], list[object]]:
            events: list[object] = []

            class Value:
                def __len__(self) -> int:
                    events.append('len')
                    return 20

                def __getitem__(self, key: slice) -> str:
                    events.append(('slice', key.start, key.stop, key.step))
                    return 'foo bar '

            environment = jinja2.Environment()
            values = {'value': Value(), 'kill': killwords}
            session = (docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), values)
                       if native else None)
            environment.policies['truncate.leeway'] = 0
            source = '{{ value|truncate(8, kill, "..") }}'
            if session is None:
                result = observe(source, values, False, environment)
            else:
                result = ('result', session.render('<string>', source), '')
            return result, events

        for killwords in (False, True):
            with self.subTest(killwords=killwords):
                expected = run(False, killwords)
                with no_python_truncate():
                    actual = run(True, killwords)
                self.assertEqual(actual, expected)

    def test_complete_document(self) -> None:
        source = '{{ value|truncate(9, true, "..", 0) }}'
        values = {'value': 'Café, beautiful world'}
        reference = generate(docxtpl, source, values)
        with no_python_truncate():
            candidate = generate(docxtpl_native, source, values)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_values_and_runtime_arguments(self) -> None:
        source = '{{ value|truncate(length, kill, end, leeway) }}'
        for value in ('', 'a b c d e f', 'é 😀 more words', 'a\ud800bcdef'):
            for length in (0, 2, 5, 12):
                for kill in (False, True):
                    for end in ('', '..'):
                        for leeway in (0, 3):
                            values = {'value': value, 'length': length, 'kill': kill,
                                      'end': end, 'leeway': leeway}
                            with self.subTest(values=values):
                                expected = observe(source, values, False)
                                with no_python_truncate():
                                    actual = observe(source, values, True)
                                self.assertEqual(actual, expected)

    def test_replaced_len_and_assertion_formatting(self) -> None:
        original = filters.__dict__.get('len')
        had_original = 'len' in filters.__dict__

        def run(native: bool) -> tuple[tuple[str, str, str], list[object]]:
            events: list[object] = []

            class Length(int):
                def __ge__(self, other: object) -> bool:
                    events.append(('ge', other))
                    return False

                def __format__(self, spec: str) -> str:
                    events.append(('format', spec))
                    return 'custom-length'

            def replacement(value: Any) -> int:
                events.append(('len', value))
                return len(value)

            filters.__dict__['len'] = replacement
            try:
                result = observe('{{ value|truncate(length, end="...") }}',
                                 {'value': 'abcdef', 'length': Length(2)}, native)
                return result, events
            finally:
                if had_original:
                    filters.__dict__['len'] = original
                else:
                    del filters.__dict__['len']

        expected = run(False)
        with no_python_truncate():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_optimized_python_disables_reference_assertions(self) -> None:
        source = r'''
import jinja2
import docxtpl_native
import sys
from docxtpl_native.bridge import BridgeConfig
env = jinja2.Environment()
source = '{{ value|truncate(2, end="...", leeway=0) }}'
context = {'value': 'abcd'}
expected = env.from_string(source).render(context)
session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(env, False), context)
calls = []
def profile(frame, event, argument):
    if event == 'call' and frame.f_code is jinja2.filters.do_truncate.__code__:
        calls.append(frame.f_code.co_name)
sys.setprofile(profile)
actual = session.render('<string>', source)
sys.setprofile(None)
print(repr(expected))
print(repr(actual))
print(expected)
print(calls)
'''
        process = subprocess.run([sys.executable, '-O', '-c', source], capture_output=True, text=True, check=True)
        lines = process.stdout.splitlines()
        self.assertEqual(lines[0], lines[1])
        self.assertEqual(lines[2], 'abc...')
        self.assertEqual(lines[3], '[]')
