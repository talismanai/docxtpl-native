"""Differential checks for optimized arithmetic involving non-finite values."""

import builtins
import io
import itertools
import unittest
from typing import Any

import docxtpl
import docxtpl_native
from jinja2 import Environment
from jinja2.sandbox import SandboxedEnvironment
from paritylab.compare import compare_docx
from paritylab.protocol_base import template
from test_native_filter_evaluation import render


class NonfiniteOptimizationTests(unittest.TestCase):
    def test_numeric_expression_matrix(self) -> None:
        def run(native: bool, expression: str) -> tuple[object, ...]:
            try:
                output = render('{{ capture(' + expression + ') }}', {'capture': str}, native)
                return 'ok', output
            except (NameError, ZeroDivisionError, OverflowError, ValueError, TypeError) as error:
                return type(error).__name__, str(error), getattr(error, 'name', None)

        values = ('1e400', '-1e400', '0', '-1', '2.5', '(1e400 - 1e400)', '(1e400 ** 0)')
        operators = ('+', '-', '*', '/', '//', '%', '**')
        original = {name: vars(builtins)[name] for name in ('inf', 'nan') if name in vars(builtins)}
        try:
            vars(builtins).update({'inf': 42, 'nan': 17})
            for left, right, operator in itertools.product(values, values, operators):
                expression = f'({left}) {operator} ({right})'
                with self.subTest(expression=expression):
                    self.assertEqual(run(True, expression), run(False, expression))
        finally:
            for name in ('nan', 'inf'):
                vars(builtins).pop(name, None)
            vars(builtins).update(original)

    def test_complete_document(self) -> None:
        def run(engine: Any, source: bytes) -> bytes:
            document = engine.DocxTemplate(io.BytesIO(source))
            document.render({'capture': str})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue()

        existed = 'inf' in vars(builtins)
        original = vars(builtins).get('inf')
        try:
            vars(builtins)['inf'] = 42
            source = template('{{ capture(1e400 + 1) }}')
            self.assertTrue(compare_docx(run(docxtpl, source), run(docxtpl_native, source))['equivalent'])
        finally:
            if existed:
                vars(builtins)['inf'] = original
            else:
                del vars(builtins)['inf']

    def test_sandbox_policy_attribute_accesses(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name in ('sandboxed', 'intercepted_binops'):
                        events.append(name)
                    return super().__getattribute__(name)

            return render('{{ capture(1e400 + 1) }}', {'capture': str}, native, Sandbox()), events

        existed = 'inf' in vars(builtins)
        original = vars(builtins).get('inf')
        try:
            vars(builtins)['inf'] = 42
            self.assertEqual(run(True), run(False))
        finally:
            if existed:
                vars(builtins)['inf'] = original
            else:
                del vars(builtins)['inf']

    def test_finite_folded_result_keeps_identity(self) -> None:
        def run(native: bool, source: str) -> tuple[str, list[bool]]:
            seen: list[object] = []

            def capture(value: object) -> str:
                seen.append(value)
                return 'x'

            output = render(source, {'capture': capture, 'items': range(3)}, native)
            return output, [seen[0] is item for item in seen]

        for source in ('{{ capture(1e400**0) }}{{ capture(1e400**0) }}',
                       '{% for item in items %}{{ capture(1e400**0) }}{% endfor %}'):
            with self.subTest(source=source):
                self.assertEqual(run(True, source), run(False, source))

    def test_arithmetic_and_optimization_policy(self) -> None:
        def run(native: bool, expression: str, optimized: bool) -> tuple[object, ...]:
            events: list[object] = []

            def capture(value: object) -> str:
                events.append((type(value).__name__, value))
                return str(value)

            try:
                output = render('{{ capture(' + expression + ') }}', {'capture': capture}, native,
                                Environment(optimized=optimized))
                return 'ok', output, events
            except (NameError, ZeroDivisionError, OverflowError) as error:
                return type(error).__name__, str(error), getattr(error, 'name', None), events

        expressions = ('1e400 + 1', '1e400 - 1e400', '1e400 * 0',
                       '1e400 / 2', '1e400 // 2', '1e400 % 2',
                       '1e400 ** 0', '1e400 / 0', '(1e400 + 1) * 2',
                       '1e400 + (5 // 2)', '-(1e400 + 2)')
        original = {name: vars(builtins)[name] for name in ('inf', 'nan') if name in vars(builtins)}
        try:
            for name in ('inf', 'nan'):
                vars(builtins).pop(name, None)
            for replacement in (False, True):
                if replacement:
                    vars(builtins).update({'inf': 42, 'nan': 17})
                for optimized in (False, True):
                    for expression in expressions:
                        with self.subTest(expression=expression, optimized=optimized, replacement=replacement):
                            self.assertEqual(run(True, expression, optimized), run(False, expression, optimized))
        finally:
            for name in ('inf', 'nan'):
                vars(builtins).pop(name, None)
            vars(builtins).update(original)

    def test_sandbox_interception(self) -> None:
        def run(native: bool, intercepted: bool) -> tuple[str, list[object]]:
            events: list[object] = []

            class Sandbox(SandboxedEnvironment):
                def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
                    events.append((operator, left, right))
                    return super().call_binop(context, operator, left, right)

            environment = Sandbox()
            environment.intercepted_binops = frozenset({'+'}) if intercepted else frozenset()
            output = render('{{ capture(1e400 + 1) }}', {'capture': str}, native, environment)
            return output, events

        existed = 'inf' in vars(builtins)
        original = vars(builtins).get('inf')
        try:
            vars(builtins)['inf'] = 42
            for intercepted in (False, True):
                with self.subTest(intercepted=intercepted):
                    self.assertEqual(run(True, intercepted), run(False, intercepted))
        finally:
            if existed:
                vars(builtins)['inf'] = original
            else:
                del vars(builtins)['inf']

    def test_nested_unary_and_binary_interception(self) -> None:
        def run(native: bool, unary: bool, binary: bool, asynchronous: bool) -> tuple[str, list[object]]:
            events: list[object] = []

            class Sandbox(SandboxedEnvironment):
                def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
                    events.append(('binary', operator, left, right))
                    return super().call_binop(context, operator, left, right)

                def call_unop(self, context: Any, operator: str, arg: Any) -> Any:
                    events.append(('unary', operator, arg))
                    return super().call_unop(context, operator, arg)

            environment = Sandbox(enable_async=asynchronous)
            environment.intercepted_unops = frozenset({'-'}) if unary else frozenset()
            environment.intercepted_binops = frozenset({'+'}) if binary else frozenset()
            output = render('{{ capture(-(1e400 + 1)) }}', {'capture': str}, native, environment)
            return output, events

        existed = 'inf' in vars(builtins)
        original = vars(builtins).get('inf')
        try:
            vars(builtins)['inf'] = 42
            for unary in (False, True):
                for binary in (False, True):
                    for asynchronous in (False, True):
                        with self.subTest(unary=unary, binary=binary, asynchronous=asynchronous):
                            self.assertEqual(run(True, unary, binary, asynchronous),
                                             run(False, unary, binary, asynchronous))
        finally:
            if existed:
                vars(builtins)['inf'] = original
            else:
                del vars(builtins)['inf']
