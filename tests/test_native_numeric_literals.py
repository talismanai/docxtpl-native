"""Numeric literal types preserved across calls into Python objects."""

import math
import unittest
from typing import Any

from test_native_filter_evaluation import render


class NumericLiteralTests(unittest.TestCase):
    def test_float_types_and_signs_at_callback_boundary(self) -> None:
        expressions = ('0.0', '-0.0', '42.0', '-1.0', '1.25', '1e20')
        for expression in expressions:
            def run(native: bool, expression: str = expression) -> tuple[str, list[tuple[str, float]]]:
                seen: list[tuple[str, float]] = []

                def callback(value: Any) -> str:
                    seen.append((type(value).__name__, math.copysign(1.0, value)))
                    return repr(value)

                return render('{{ callback(' + expression + ') }}', {'callback': callback}, native), seen

            with self.subTest(expression=expression):
                expected = run(False)
                self.assertEqual(expected[1][0][0], 'float')
                self.assertEqual(run(True), expected)
