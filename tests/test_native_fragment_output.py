"""Differential checks for fragment-preserving output and capture boundaries."""

import unittest
from itertools import product
from typing import Any

import jinja2
from test_native_filter_evaluation import render


class FragmentOutputTests(unittest.TestCase):
    def test_generated_group_boundaries(self) -> None:
        parts = ('A<', '{{ value }}', '{{ empty }}', '{{ 12 }}', '{{ "a\\ud800b" }}',
                 '{% if flag %}B{% endif %}', '{% set n=1 %}',
                 '{% for x in [1,2] %}z{% endfor %}')
        context = {'value': 'v&', 'empty': '', 'flag': True}

        def observe(source: str, native: bool, asynchronous: bool, autoescape: bool) -> tuple[str, str]:
            try:
                return ('result', render(source, context, native,
                                         jinja2.Environment(enable_async=asynchronous, autoescape=autoescape)))
            except TypeError as error:
                return ('TypeError', str(error))

        for asynchronous in (False, True):
            for autoescape in (False, True):
                for combination in product(parts, repeat=3):
                    source = ''.join(combination) + '{% filter length %}abc{% endfilter %}'
                    with self.subTest(asynchronous=asynchronous, autoescape=autoescape, source=source):
                        self.assertEqual(observe(source, True, asynchronous, autoescape),
                                         observe(source, False, asynchronous, autoescape))

    def test_filter_block_error_positions_follow_jinja_output_groups(self) -> None:
        sources = (
            'A{{ value }}{% filter length %}abc{% endfilter %}',
            '{{ value }}{{ value }}{% filter length %}abc{% endfilter %}',
            'A{{ 1 }}B{{ value }}C{% filter length %}abc{% endfilter %}',
            'A{% if flag %}B{% endif %}C{% filter length %}abc{% endfilter %}',
            'A{% set n=1 %}B{% filter length %}abc{% endfilter %}',
            'A{{ "a\\ud800b" }}B{% filter length %}abc{% endfilter %}',
            '{{ empty }}{% filter length %}abc{% endfilter %}',
            '{{ "" }}{% filter length %}abc{% endfilter %}',
            '{% for item in [1, 2] %}x{% endfor %}{% filter length %}abc{% endfilter %}',
            '{% macro m() %}A{{ value }}{% filter length %}abc{% endfilter %}{% endmacro %}{{ m() }}',
        )

        def observe(source: str, native: bool, asynchronous: bool) -> tuple[str, str]:
            environment = jinja2.Environment(enable_async=asynchronous)
            try:
                return ('result', render(source, {'value': 'x', 'empty': '', 'flag': True}, native, environment))
            except TypeError as error:
                return ('TypeError', str(error))

        for asynchronous in (False, True):
            for source in sources:
                with self.subTest(asynchronous=asynchronous, source=source):
                    self.assertEqual(observe(source, True, asynchronous), observe(source, False, asynchronous))

    def test_surrogate_output_and_nested_captures(self) -> None:
        sources = (
            'before{{ value }}after',
            '{{ "a\\ud800b" }}',
            '{% set captured %}{{ value }}{% endset %}{{ captured|length }}:{{ captured }}',
            '{% macro m() %}{{ value }}{% endmacro %}{{ m()|length }}:{{ m() }}',
            '{% for item in [value, "x"] %}{{ item }}{% endfor %}',
        )
        for asynchronous in (False, True):
            for autoescape in (False, True):
                for source in sources:
                    with self.subTest(asynchronous=asynchronous, autoescape=autoescape, source=source):
                        environment = jinja2.Environment(enable_async=asynchronous, autoescape=autoescape)
                        expected = render(source, {'value': 'a\ud800b'}, False, environment)
                        actual = render(source, {'value': 'a\ud800b'}, True, environment)
                        self.assertEqual(actual, expected)

    def test_compile_time_finalizer_is_not_repeated(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def finalize(value: Any) -> Any:
                events.append(ascii(value))
                return value

            environment = jinja2.Environment(enable_async=asynchronous, finalize=finalize)
            result = render('{{ "a\\ud800b" }}{{ value }}', {'value': 'x'}, native, environment)
            return result, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_filter_block_does_not_stringify_or_finalize_its_result(self) -> None:
        def observe(native: bool, asynchronous: bool, nested: bool) -> tuple[object, list[str]]:
            events: list[str] = []

            class Foreign:
                def __str__(self) -> str:
                    events.append('str')
                    return 'foreign'

            def filter_value(value: str) -> Foreign:
                events.append('filter:' + value)
                return Foreign()

            def finalize(value: Any) -> Any:
                events.append('finalize:' + str(value))
                return value

            def later() -> str:
                events.append('later')
                return '!'

            environment = jinja2.Environment(enable_async=asynchronous, finalize=finalize)
            environment.filters['foreign'] = filter_value
            block = '{% filter foreign %}inside{% endfilter %}'
            source = ('{% macro m() %}' + block + '{% endmacro %}{{ m() }}' if nested else block) + '{{ later() }}'
            try:
                output: object = render(source, {'later': later}, native, environment)
            except TypeError as error:
                output = (type(error).__name__, str(error))
            return output, events

        for asynchronous in (False, True):
            for nested in (False, True):
                with self.subTest(asynchronous=asynchronous, nested=nested):
                    self.assertEqual(observe(True, asynchronous, nested), observe(False, asynchronous, nested))
