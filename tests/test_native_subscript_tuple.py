"""Differential tests for Jinja's multi-item subscript expressions."""

import math
import unittest
from typing import Any

import docxtpl
import docxtpl_native
import jinja2
from jinja2.sandbox import SandboxedEnvironment
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template
from test_native_filter_evaluation import render


class MultiSubscriptTests(unittest.TestCase):
    def test_same_named_loader_keeps_tuple_constant_identity(self) -> None:
        def run(native: bool, asynchronous: bool, mode: str) -> tuple[str, list[list[bool]]]:
            from docxtpl_native.bridge import BridgeConfig

            keys: list[object] = []

            class Value:
                def __getitem__(self, key: object) -> str:
                    keys.append(key)
                    return 'x'

            name = 'word/document.xml'
            environment = jinja2.Environment(
                loader=jinja2.DictLoader({name: '{{ value[1, 2] }}'}),
                auto_reload=False, enable_async=asynchronous,
            )
            source = "{{ value[1, 2] }}{% include 'word/document.xml' %}{{ value[1, 2] }}"
            if mode == 'block':
                source = '{% block body %}' + source + '{% endblock %}'
            elif mode == 'macro':
                source = ("{% macro item() %}{{ value[1, 2] }}{% endmacro %}"
                          "{{ item() }}{% include 'word/document.xml' %}{{ item() }}")
            context = {'value': Value()}
            if native:
                config = BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, context)
                output = session.render(name, source) + session.render(name, source)
            else:
                output = environment.from_string(source).render(context) + environment.from_string(source).render(context)
            return output, [[left is right for right in keys] for left in keys]

        for asynchronous in (False, True):
            for mode in ('root', 'block', 'macro'):
                with self.subTest(asynchronous=asynchronous, mode=mode):
                    expected = run(False, asynchronous, mode)
                    self.assertEqual(expected[0], 'xxxxxx')
                    self.assertEqual([expected[1][left][right] for left, right in
                                      ((0, 2), (0, 1), (1, 4), (0, 3), (3, 5))],
                                     [True, False, True, False, True])
                    self.assertEqual(run(True, asynchronous, mode), expected)

    def test_dynamic_keys_and_callback_order(self) -> None:
        def run(native: bool, fail: bool) -> tuple[Any, list[object]]:
            events: list[object] = []
            failure = ValueError('second failed')

            class Value:
                def __getitem__(self, key: object) -> str:
                    events.append(('getitem', type(key), key))
                    return 'ok'

            def first() -> int:
                events.append('first')
                return 1

            def second() -> int:
                events.append('second')
                if fail:
                    raise failure
                return 2

            try:
                result: Any = ('result', render('{{ value[first(), second()] }}',
                                               {'value': Value(), 'first': first, 'second': second}, native))
            except ValueError as error:
                result = ('error', type(error).__name__, str(error), error is failure)
            return result, events

        for fail in (False, True):
            with self.subTest(fail=fail):
                expected = run(False, fail)
                self.assertEqual(expected[0][0], 'error' if fail else 'result')
                self.assertEqual(run(True, fail), expected)

    def test_complete_document_with_same_named_loader_tuple_identity(self) -> None:
        source = "{{ value[1, 2] }}{% include 'word/document.xml' %}{{ value[1, 2] }}"
        document = template(source)

        def run(engine: Any, asynchronous: bool) -> bytes:
            keys: list[object] = []

            class Value:
                def __getitem__(self, key: object) -> str:
                    keys.append(key)
                    return str(key is keys[0])

            environment = jinja2.Environment(
                loader=jinja2.DictLoader({'word/document.xml': '{{ value[1, 2] }}'}),
                auto_reload=False, enable_async=asynchronous,
            )
            return generate(engine, source, {'value': Value()}, environment=environment, source=document)

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                comparison = compare_docx(run(docxtpl, asynchronous), run(docxtpl_native, asynchronous))
                self.assertTrue(comparison['equivalent'], comparison)

    def test_constant_key_identity_in_loop(self) -> None:
        def run(native: bool, source: str) -> tuple[str, list[object]]:
            keys: list[object] = []

            class Value:
                def __getitem__(self, key: object) -> str:
                    keys.append(key)
                    return 'x'

            output = render(source, {'value': Value(), 'items': [1, 2, 3]}, native)
            return output, [type(key) for key in keys] + [keys[0] is key for key in keys]

        sources = ('{% for item in items %}{{ value[1, 2] }}{% endfor %}',
                   '{{ value[1, 2] }}{{ value[1, 2] }}',
                   ('{% macro a() %}{{ value[1, 2] }}{% endmacro %}'
                    '{% macro b() %}{{ value[1, 2] }}{% endmacro %}{{ a() }}{{ b() }}'),
                   '{{ value[true, 2] }}{{ value[1, 2] }}',
                   '{{ value[-1, +2] }}{{ value[-1, +2] }}',
                   '{{ value[-0.0, 2] }}{{ value[0.0, 2] }}')
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(run(True, source), run(False, source))

    def test_complete_document_with_tuple_key(self) -> None:
        source = '{{ value[1, 2] }}'
        input_doc = template(source)
        values = {'value': {(1, 2): 'found'}}
        reference = generate(docxtpl, source, values, source=input_doc)
        candidate = generate(docxtpl_native, source, values, source=input_doc)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_dynamic_tuple_keys_are_not_interned(self) -> None:
        def run(native: bool) -> tuple[str, list[bool]]:
            keys: list[object] = []

            class Value:
                def __getitem__(self, key: object) -> str:
                    keys.append(key)
                    return 'x'

            output = render('{% for item in items %}{{ value[item, 2] }}{% endfor %}',
                            {'value': Value(), 'items': [1, 1, 1]}, native)
            return output, [keys[0] is keys[1], keys[1] is keys[2]]

        expected = run(False)
        self.assertEqual(expected, ('xxx', [False, False]))
        self.assertEqual(run(True), expected)

    def test_signed_tuple_identity_and_sandbox_interception(self) -> None:
        def run(native: bool, intercepted: frozenset[str], asynchronous: bool) -> tuple[str, list[object]]:
            events: list[object] = []
            keys: list[object] = []

            class Value:
                def __getitem__(self, key: object) -> str:
                    keys.append(key)
                    events.append(('key', key))
                    return 'x'

            class Environment(SandboxedEnvironment):
                def call_unop(self, context: Any, operator: str, arg: Any) -> Any:
                    events.append(('unop', operator, arg))
                    return super().call_unop(context, operator, arg)

            environment = Environment(enable_async=asynchronous)
            environment.intercepted_unops = intercepted
            source = '{% for item in items %}{{ value[-1, +2] }}{% endfor %}'
            output = render(source, {'value': Value(), 'items': [1, 2, 3]}, native, environment)
            return output, events + [keys[0] is keys[1], keys[1] is keys[2]]

        for intercepted in (frozenset(), frozenset({'-'}), frozenset({'+'}), frozenset({'-', '+'})):
            for asynchronous in (False, True):
                with self.subTest(intercepted=intercepted, asynchronous=asynchronous):
                    expected = run(False, intercepted, asynchronous)
                    self.assertEqual(expected[0], 'xxx')
                    self.assertEqual(expected[1][-2:], [not intercepted, not intercepted])
                    self.assertEqual(run(True, intercepted, asynchronous), expected)

    def test_float_literal_type_and_zero_sign_in_tuple(self) -> None:
        def run(native: bool) -> tuple[str, list[object]]:
            observed: list[object] = []

            class Value:
                def __getitem__(self, key: Any) -> str:
                    observed.append(tuple((type(value).__name__, value, math.copysign(1.0, value)) for value in key))
                    return 'x'

            source = '{{ value[0.0, -0.0, 42.0, 1.25] }}'
            return render(source, {'value': Value()}, native), observed

        expected = run(False)
        self.assertEqual(expected[1], [(('float', 0.0, 1.0), ('float', -0.0, -1.0),
                                       ('float', 42.0, 1.0), ('float', 1.25, 1.0))])
        self.assertEqual(run(True), expected)
