"""Differential coverage for optimized finite numeric expressions."""

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


class FiniteOptimizationTests(unittest.TestCase):
    def test_numeric_expression_matrix(self) -> None:
        def run(native: bool, expression: str) -> tuple[object, ...]:
            def capture(value: object) -> tuple[str, str]:
                return type(value).__name__, repr(value)

            try:
                return 'ok', render('{{ capture(' + expression + ') }}', {'capture': capture}, native)
            except (NameError, ZeroDivisionError, OverflowError, ValueError, TypeError) as error:
                return type(error).__name__, str(error), getattr(error, 'name', None)

        values = ('0', '1', '-1', '2.5', '-2.5', '1000', '1e308')
        for left, right, operator in itertools.product(values, values, ('+', '-', '*', '/', '//', '%', '**')):
            expression = f'({left}) {operator} ({right})'
            with self.subTest(expression=expression):
                self.assertEqual(run(True, expression), run(False, expression))

    def test_document_output_depends_on_folded_identity(self) -> None:
        source = '{{ capture(1000) }}{{ capture(1000 + 0) }}'
        document = template(source)

        def run(engine: Any) -> bytes:
            seen: list[object] = []

            def capture(value: object) -> str:
                seen.append(value)
                return str(value is seen[0])

            return generate(engine, source, {'capture': capture}, source=document)

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_folded_numeric_identity(self) -> None:
        def run(native: bool, expression: str, literal: str, asynchronous: bool) -> tuple[str, list[str], list[bool]]:
            seen: list[Any] = []

            def capture(value: Any) -> str:
                seen.append(value)
                return 'x'

            source = '{{ capture(' + literal + ') }}{% for item in items %}{{ capture(' + expression + ') }}{% endfor %}'
            output = render(source, {'capture': capture, 'items': range(2)}, native,
                            Environment(enable_async=asynchronous))
            return output, [repr(value) for value in seen], [seen[0] is value for value in seen]

        cases = (('1000 + 0', '1000'), ('2000 - 1000', '1000'), ('40 * 25', '1000'),
                 ('2000 / 2', '1000.0'), ('2001 // 2', '1000'), ('2001 % 1001', '1000'),
                 ('10 ** 3', '1000'), ('(20 + 20) * 25', '1000'), ('-500 - 500', '-1000'),
                 ('0.0 * -1', '-0.0'))
        for expression, literal in cases:
            for asynchronous in (False, True):
                with self.subTest(expression=expression, asynchronous=asynchronous):
                    self.assertEqual(run(True, expression, literal, asynchronous),
                                     run(False, expression, literal, asynchronous))

    def test_overflow_to_infinity_and_failed_folds(self) -> None:
        def run(native: bool, expression: str) -> tuple[object, ...]:
            trace: list[str] = []

            def capture(value: object) -> str:
                trace.append(repr(value))
                return str(value)

            try:
                result = render('{{ capture(' + expression + ') }}', {'capture': capture}, native)
                return 'ok', result, trace
            except (NameError, ZeroDivisionError, OverflowError) as error:
                return type(error).__name__, str(error), getattr(error, 'name', None), trace

        original = vars(builtins).get('inf')
        existed = 'inf' in vars(builtins)
        try:
            vars(builtins).pop('inf', None)
            for replaced in (False, True):
                if replaced:
                    vars(builtins)['inf'] = 42
                for expression in ('1e308 * 1e308', '1 / 0', '0 ** -1', '10 ** 1000'):
                    with self.subTest(expression=expression, replaced=replaced):
                        self.assertEqual(run(True, expression), run(False, expression))
        finally:
            if existed:
                vars(builtins)['inf'] = original
            else:
                vars(builtins).pop('inf', None)

    def test_intercepted_operator_and_nested_fold(self) -> None:
        def run(native: bool, intercepted: bool, asynchronous: bool) -> tuple[str, list[object]]:
            events: list[object] = []

            class Sandbox(SandboxedEnvironment):
                def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
                    events.append((operator, left, right))
                    return super().call_binop(context, operator, left, right)

            environment = Sandbox(enable_async=asynchronous)
            environment.intercepted_binops = frozenset({'+'}) if intercepted else frozenset()
            output = render('{% for x in items %}{{ capture((500 + 500) * 2) }}{% endfor %}',
                            {'capture': str, 'items': range(2)}, native, environment)
            return output, events

        for intercepted in (False, True):
            for asynchronous in (False, True):
                with self.subTest(intercepted=intercepted, asynchronous=asynchronous):
                    self.assertEqual(run(True, intercepted, asynchronous), run(False, intercepted, asynchronous))
