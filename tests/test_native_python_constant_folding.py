"""Differential numeric constant folding with Jinja optimization disabled."""

import builtins
import itertools
import unittest
from typing import Any

import docxtpl
import docxtpl_native
from jinja2 import Environment
from jinja2.sandbox import SandboxedEnvironment
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template
from test_native_filter_evaluation import render


class PythonConstantFoldingTests(unittest.TestCase):
    def test_complete_document_with_optimization_disabled(self) -> None:
        source = '{{ capture(1000) }}{{ capture(1000 + 0) }}'
        document = template(source)

        def run(engine: Any) -> bytes:
            values: list[object] = []

            def capture(value: object) -> str:
                values.append(value)
                return str(values[0] is value)

            return generate(engine, source, {'capture': capture}, Environment(optimized=False), source=document)

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_numeric_expression_matrix(self) -> None:
        def run(native: bool, expression: str) -> tuple[object, ...]:
            def capture(value: object) -> tuple[str, str]:
                return type(value).__name__, repr(value)

            try:
                return 'ok', render('{{ capture(' + expression + ') }}', {'capture': capture}, native,
                                    Environment(optimized=False))
            except (NameError, ZeroDivisionError, OverflowError, ValueError, TypeError) as error:
                return type(error).__name__, str(error), getattr(error, 'name', None)

        values = ('0', '1', '-1', '2.5', '-2.5', '1000', '1e308')
        for left, right, operator in itertools.product(values, values, ('+', '-', '*', '/', '//', '%', '**')):
            expression = f'({left}) {operator} ({right})'
            with self.subTest(expression=expression):
                self.assertEqual(run(True, expression), run(False, expression))

    def test_nan_identity_per_source_site(self) -> None:
        def run(native: bool, source: str) -> list[list[bool]]:
            values: list[float] = []

            def capture(value: float) -> str:
                values.append(value)
                return ''

            render(source, {'capture': capture, 'items': range(2)}, native, Environment(optimized=False))
            return [[left is right for right in values] for left in values]

        expression = '(1e308 * 1e308) - (1e308 * 1e308)'
        for source in ('{{ capture(' + expression + ') }}{{ capture(' + expression + ') }}',
                       '{% for item in items %}{{ capture(' + expression + ') }}{% endfor %}',
                       ('{% macro m() %}{{ capture(' + expression + ') }}{% endmacro %}'
                        '{{ capture(' + expression + ') }}{{ m() }}{{ m() }}')):
            with self.subTest(source=source):
                self.assertEqual(run(True, source), run(False, source))

    def test_integer_power_folding_limit(self) -> None:
        def run(native: bool, exponent: int) -> list[bool]:
            values: list[int] = []

            def capture(value: int) -> str:
                values.append(value)
                return ''

            source = '{% for item in items %}{{ capture(2 ** ' + str(exponent) + ') }}{% endfor %}'
            render(source, {'capture': capture, 'items': range(2)}, native, Environment(optimized=False))
            return [values[0] is value for value in values]

        for exponent in (63, 64, 65, 128):
            with self.subTest(exponent=exponent):
                self.assertEqual(run(True, exponent), run(False, exponent))

    def test_finite_arithmetic_and_identity(self) -> None:
        def run(native: bool, source: str, asynchronous: bool) -> tuple[str, list[tuple[str, str]], list[bool]]:
            values: list[Any] = []

            def capture(value: Any) -> str:
                values.append(value)
                return 'x'

            output = render(source, {'capture': capture, 'items': range(2)}, native,
                            Environment(optimized=False, enable_async=asynchronous))
            return output, [(type(value).__name__, repr(value)) for value in values], [values[0] is value for value in values]

        cases = (('1000', '1000 + 0'), ('1000', '2000 - 1000'), ('1000', '40 * 25'),
                 ('1000.0', '2000 / 2'), ('1000', '2001 // 2'), ('1000', '2001 % 1001'),
                 ('1000', '10 ** 3'), ('1000', '(20 + 20) * 25'), ('-1000', '-500 - 500'),
                 ('-0.0', '0.0 * -1'))
        for literal, expression in cases:
            source = '{{ capture(' + literal + ') }}{% for item in items %}{{ capture(' + expression + ') }}{% endfor %}'
            for asynchronous in (False, True):
                with self.subTest(expression=expression, asynchronous=asynchronous):
                    self.assertEqual(run(True, source, asynchronous), run(False, source, asynchronous))

    def test_float_overflow_is_a_float_constant(self) -> None:
        def run(native: bool) -> tuple[str, list[bool]]:
            values: list[float] = []

            def capture(value: float) -> str:
                values.append(value)
                return str(value)

            source = '{% for item in items %}{{ capture(1e308 * 1e308) }}{% endfor %}'
            output = render(source, {'capture': capture, 'items': range(2)}, native, Environment(optimized=False))
            return output, [values[0] is value for value in values]

        existed = 'inf' in vars(builtins)
        original = vars(builtins).get('inf')
        try:
            vars(builtins).pop('inf', None)
            for replaced in (False, True):
                if replaced:
                    vars(builtins)['inf'] = 42
                with self.subTest(replaced=replaced):
                    self.assertEqual(run(True), run(False))
        finally:
            if existed:
                vars(builtins)['inf'] = original
            else:
                vars(builtins).pop('inf', None)

    def test_intercepted_operator_is_not_folded(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[object]]:
            events: list[object] = []

            class Sandbox(SandboxedEnvironment):
                def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
                    events.append((operator, left, right))
                    return super().call_binop(context, operator, left, right)

            environment = Sandbox(optimized=False, enable_async=asynchronous)
            environment.intercepted_binops = frozenset({'+'})
            output = render('{% for item in items %}{{ capture(500 + 500) }}{% endfor %}',
                            {'capture': str, 'items': range(2)}, native, environment)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(run(True, asynchronous), run(False, asynchronous))
