"""Differential checks for Python object protocols used by native operators."""
import operator
import unittest
from collections.abc import Iterator
from unittest.mock import patch

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig


@unittest.skipUnless(docxtpl_native.capabilities()['native_available'], 'native extension unavailable')
class NativeOperatorDispatchTests(unittest.TestCase):
    def render(self, native, source, context):
        environment = jinja2.Environment()
        if native:
            session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
            return session.render('case', source)
        return environment.from_string(source).render(context)

    def test_runtime_operators_do_not_depend_on_monkeypatched_operator_module(self):
        source = '{{ -value }}|{{ +value }}|{{ key in values }}|{{ key not in values }}'
        context = {'value': 4, 'key': 2, 'values': [1, 2]}
        def wrong(*args):
            raise AssertionError('operator module must not control Jinja bytecode')
        with patch.object(operator, 'neg', wrong), patch.object(operator, 'pos', wrong), patch.object(operator, 'contains', wrong):
            expected = self.render(False, source, context)
            self.assertEqual(expected, '-4|4|True|False')
            self.assertEqual(self.render(True, source, context), expected)

    def test_reflected_arithmetic_and_rich_comparison_keep_identity_and_order(self):
        def run(native):
            trace = []
            class Result:
                def __bool__(self):trace.append('bool');return True
                def __str__(self):trace.append('str');return 'result'
            result = Result()
            class Value:
                def __radd__(self, other):trace.append(('radd', other));return 12
                def __lt__(self, other):trace.append(('lt', other));return result
            def capture(value):trace.append(('identity', value is result));return value
            text = self.render(native, '{{ number + value }}|{{ capture(value < number) }}',
                               {'number': 3, 'value': Value(), 'capture': capture})
            return text, trace
        self.assertEqual(run(True), run(False))

    def test_numeric_and_comparison_operations_match_python(self):
        source = ('{{ a+b }}|{{ a-b }}|{{ a*b }}|{{ a/b }}|{{ a//b }}|{{ a%b }}|{{ a**b }}|'
                  '{{ a==b }}|{{ a!=b }}|{{ a<b }}|{{ a<=b }}|{{ a>b }}|{{ a>=b }}')
        for a, b in ((-17, 3), (17, -3), (2 ** 120 + 3, 5), (2.5, 1.25)):
            with self.subTest(a=a, b=b):
                context = {'a': a, 'b': b}
                self.assertEqual(self.render(True, source, context), self.render(False, source, context))

    def test_membership_slice_and_original_exception(self):
        def run(native, fail):
            trace = []
            failure = ValueError('operator failure')
            class Source:
                def __contains__(self, item):
                    trace.append(('contains', item))
                    if fail:raise failure
                    return True
                def __getitem__(self, key):
                    trace.append(('slice', type(key) is slice, key.start, key.stop, key.step))
                    return 'item'
            try:
                text = self.render(native, '{{ key in source }}|{{ source[begin:end:step] }}',
                                   {'key': 'x', 'source': Source(), 'begin': 1, 'end': 7, 'step': 2})
                return ('ok', text, trace)
            except Exception as error:
                return ('error', error is failure, str(error), trace)
        for fail in (False, True):
            with self.subTest(fail=fail):self.assertEqual(run(True, fail), run(False, fail))

    def test_membership_falls_back_to_iterator(self):
        def run(native):
            trace = []
            class Values:
                def __iter__(self):
                    for value in (1, 2, 3):
                        trace.append(value)
                        yield value
            text = self.render(native, '{{ item in values }}', {'item': 2, 'values': Values()})
            return text, trace
        self.assertEqual(run(True), run(False))

    def test_membership_iterator_error_and_short_circuit(self) -> None:
        def run(native: bool, expression: str, item: int) -> tuple[object, ...]:
            trace: list[int] = []
            failure = ValueError('iterator failed')

            class Values:
                def __iter__(self) -> Iterator[int]:
                    trace.append(1)
                    yield 1
                    trace.append(2)
                    raise failure

            try:
                text = self.render(native, '{{ ' + expression + ' }}', {'item': item, 'values': Values()})
                return 'ok', text, trace
            except ValueError as error:
                return 'error', error is failure, trace

        for expression in ('item in values', 'item not in values', '0 < item in values', '0 < item not in values'):
            for item in (1, 2):
                with self.subTest(expression=expression, item=item):
                    expected = run(False, expression, item)
                    if item == 1:
                        self.assertEqual(expected[2], [1])
                    else:
                        self.assertEqual(expected, ('error', True, [1, 2]))
                    self.assertEqual(run(True, expression, item), expected)

    def test_binding_keeps_slices_and_unknown_operator_behavior(self):
        config = BridgeConfig(jinja2.Environment(), False)
        self.assertEqual(config.operate('slice', 'abcdef', [1, 5, 2], False), 'bd')
        self.assertEqual(config.operate('slice', 'abcdef', [3], False), 'abc')
        with self.assertRaises(KeyError) as error:
            config.operate('unknown', 1, 2, False)
        self.assertEqual(error.exception.args, ('unknown',))


if __name__ == '__main__':
    unittest.main()
