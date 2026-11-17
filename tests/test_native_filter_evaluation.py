"""Differential outputs, effects and proof of the migrated execution boundary."""
from contextlib import contextmanager
import sys
from types import SimpleNamespace
import unittest

import docxtpl
import docxtpl_native
import jinja2
from jinja2 import nodes
from jinja2 import filters
from markupsafe import Markup
from docxtpl_native.bridge import BridgeConfig
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate

FILTERS = ('upper', 'lower', 'capitalize', 'center', 'trim', 'int', 'float',
           'default', 'round', 'wordcount', 'reverse', 'last', 'safe')


def render(source, context, native, environment=None):
    environment = environment if environment is not None else jinja2.Environment()
    if native:
        session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
        return session.render('<string>', source)
    return environment.from_string(source).render(context)


@contextmanager
def no_standard_python():
    codes = {jinja2.defaults.DEFAULT_FILTERS[name].__code__ for name in FILTERS}
    codes.add(filters.do_mark_unsafe.__code__)
    codes.update(function.__code__ for function in jinja2.defaults.DEFAULT_TESTS.values()
                 if hasattr(function, '__code__'))
    codes.update(value.as_const.__code__ for value in vars(nodes).values()
                 if isinstance(value, type) and hasattr(value, 'as_const'))
    previous = sys.getprofile()
    calls = []
    def profile(frame, event, argument):
        if event == 'call' and frame.f_code in codes:
            calls.append(frame.f_code.co_qualname)
    try:
        sys.setprofile(profile)
        yield
    finally:
        sys.setprofile(previous)
    if calls:
        raise AssertionError('original Python algorithms executed: ' + repr(calls))


class NativeFilterEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        docxtpl_native._extension()

    def check(self, source, values=None, *, guarded=False):
        values = {} if values is None else values
        expected = render(source, values, False)
        if guarded:
            with no_standard_python(): actual = render(source, values, True)
        else: actual = render(source, values, True)
        self.assertEqual(actual, expected, source)
        return actual

    def test_runtime_and_compile_time_filters(self):
        for expression, value in [
            ('upper', 'Straße'), ('lower', 'Σİ'), ('capitalize', 'aBC'),
            ('center(9)', 'a'), ('trim("xy")', 'xxabcy'), ('int', '42.23'),
            ('int(7, 16)', 'ff'), ('int(7)', 'no'), ('float(3.5)', 'no'),
            ('default("missing", true)', ''), ('round(1)', 42.55),
            ('round(1,"floor")', 42.55), ('round(1,"ceil")', -42.55),
            ('wordcount', 'one_two δέκα ² a\u0301 🐱'),
        ]:
            for literal in (False, True):
                with self.subTest(expression=expression, literal=literal):
                    source = '{{ ' + (repr(value) if literal else 'value') + '|' + expression + ' }}'
                    self.check(source, {'value': value}, guarded=True)

    def test_reverse_and_last_preserve_protocols_and_errors(self):
        for expression, value in [('reverse|list', 'façade😀'), ('reverse|list', [1, 2, 3]),
                                  ('last', [1, 2, 3]), ('last|default("empty")', [])]:
            for literal in (False, True):
                source = '{{ ' + (repr(value) if literal else 'value') + '|' + expression + ' }}'
                with self.subTest(source=source): self.check(source, {'value': value}, guarded=True)
        for expression, value in [('reverse', 42), ('last', iter([1]))]:
            def observe(native):
                try: return render('{{ value|' + expression + ' }}', {'value': value}, native)
                except Exception as error:
                    cause = error.__cause__
                    return type(error).__name__, str(error), type(cause).__name__ if cause else None
            with self.subTest(expression=expression): self.assertEqual(observe(True), observe(False))

    def test_reverse_observes_global_replacements(self):
        original = getattr(filters, 'reversed', None)
        try:
            filters.reversed = lambda value: ['custom']
            self.assertEqual(self.check('{{ value|reverse|list }}', {'value': [1, 2]}, guarded=True), "['custom']")
        finally:
            if original is None: del filters.reversed
            else: filters.reversed = original

    def test_safe_and_unsafe_preserve_escaping_and_protocols(self) -> None:
        for expression in ('value|safe', 'value|unsafe', 'value|safe|unsafe',
                           'value|unsafe|safe', '"<b>"|safe', '"<b>"|unsafe'):
            for autoescape in (False, True):
                with self.subTest(expression=expression, autoescape=autoescape):
                    environment = jinja2.Environment(autoescape=autoescape)
                    environment.filters['unsafe'] = filters.do_mark_unsafe
                    source = '{{ ' + expression + ' }}'
                    expected = render(source, {'value': Markup('<b>')}, False, environment)
                    with no_standard_python():
                        actual = render(source, {'value': Markup('<b>')}, True, environment)
                    self.assertEqual(actual, expected)

    def test_safe_and_unsafe_observe_global_replacements(self) -> None:
        for name, expression in (('Markup', 'safe'), ('str', 'unsafe')):
            previous = getattr(filters, name, None)
            try:
                setattr(filters, name, lambda value: 'replacement')
                environment = jinja2.Environment()
                environment.filters['unsafe'] = filters.do_mark_unsafe
                source = '{{ value|' + expression + ' }}'
                expected = render(source, {'value': 'original'}, False, environment)
                with no_standard_python():
                    actual = render(source, {'value': 'original'}, True, environment)
                self.assertEqual(actual, expected)
                self.assertEqual(actual, 'replacement')
            finally:
                if previous is None:
                    delattr(filters, name)
                else:
                    setattr(filters, name, previous)

    def test_safe_and_unsafe_object_protocols_and_docx(self) -> None:
        def observe(native: bool, expression: str, fail: bool) -> tuple:
            events: list[str] = []

            class Value:
                def __html__(self) -> str:
                    events.append('html')
                    if fail:
                        raise ValueError('html failure')
                    return '<b>html</b>'

                def __str__(self) -> str:
                    events.append('str')
                    if fail:
                        raise ValueError('str failure')
                    return '<b>string</b>'

            environment = jinja2.Environment(autoescape=True)
            environment.filters['unsafe'] = filters.do_mark_unsafe
            try:
                result = render('{{ value|' + expression + ' }}', {'value': Value()}, native, environment)
            except ValueError as error:
                result = (type(error).__name__, str(error))
            return result, events

        for expression in ('safe', 'unsafe'):
            for fail in (False, True):
                with self.subTest(expression=expression, fail=fail):
                    expected = observe(False, expression, fail)
                    with no_standard_python():
                        actual = observe(True, expression, fail)
                    self.assertEqual(actual, expected)

        reference = generate(docxtpl, '{{ value|safe }}', {'value': 'text'})
        with no_standard_python():
            candidate = generate(docxtpl_native, '{{ value|safe }}', {'value': 'text'})
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_reverse_and_last_exception_state_and_callback_order(self):
        def observe(native, expression):
            events = []
            class Sequence:
                def __reversed__(self):
                    events.append(('reversed', type(sys.exception()).__name__))
                    raise TypeError('no reverse')
                def __iter__(self):
                    events.append(('iter', type(sys.exception()).__name__))
                    yield 1
                    yield 2
            try:
                output = render('{{ value|' + expression + ' }}', {'value': Sequence()}, native)
                return output, events
            except Exception as error:
                return (type(error).__name__, str(error),
                        type(error.__cause__).__name__ if error.__cause__ else None), events
        for expression in ('reverse|list', 'last'):
            with self.subTest(expression=expression): self.assertEqual(observe(True, expression), observe(False, expression))

    def test_lone_surrogates_in_foreign_strings(self):
        self.assertEqual(self.check('{{ value|wordcount }}', {'value': 'a\ud800b'}, guarded=True), '2')

    def test_surrogate_literals_reuse_the_foreign_ast_adapter(self):
        for literal in ('"a\\ud800b"', '"a\\uDc00b"', '"a\\U0000D800b"', '"a\\ud83d\\ude00b"', '"a\\\\ud800b"'):
            for expression in (literal + '|wordcount', literal + '|length'):
                with self.subTest(expression=expression): self.check('{{ ' + expression + ' }}', guarded=True)
        source = '{{ "a\\ud800b"|wordcount }}'
        reference = generate(docxtpl, source, {})
        with no_standard_python(): candidate = generate(docxtpl_native, source, {})
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_nested_constants_short_circuit_lookups_and_operators(self):
        for expression in [
            '[1, 2][0] + 4 * 3', '{"a": (1, 2)}.a[1]', '"abcdef"[1:5:2]',
            '"a" ~ 42 ~ none', '1 < 2 < 4', '3 in [2, 3]', '4 not in [2, 3]',
            'false and (1 / 0)', 'true or (1 / 0)', '3 if true else 1 / 0',
            'not (2 == 3)', '-2 ** 3', '5 // 2', '7 % 3',
            '"  ABC  "|trim|lower', '" 4.9 "|int|round',
        ]:
            with self.subTest(expression=expression): self.check('{{ ' + expression + ' }}', guarded=True)

    def test_all_standard_python_predicates_at_compile_and_runtime(self):
        for name, value, suffix in [
            ('defined', 1, ''), ('undefined', 1, ''), ('none', None, ''),
            ('boolean', True, ''), ('false', False, ''), ('true', True, ''),
            ('integer', 3, ''), ('float', 3.5, ''), ('string', 'a', ''),
            ('sameas', None, '(none)'), ('odd', 3, ''), ('even', 4, ''),
            ('divisibleby', 6, '(3)'), ('lower', 'abc', ''), ('upper', 'ABC', ''),
            ('mapping', {}, ''), ('number', 3, ''), ('sequence', [], ''),
            ('iterable', [], ''), ('escaped', 'a', ''), ('in', 3, '([1, 3])'),
            ('filter', 'upper', ''), ('test', 'odd', ''),
        ]:
            for constant in (False, True):
                source = '{{ ' + (repr(value) if constant else 'value') + ' is ' + name + suffix + ' }}'
                with self.subTest(name=name, constant=constant): self.check(source, {'value': value}, guarded=True)

    def test_environment_predicate_overrides_and_keyword_errors(self):
        for source in ('{{ value is filter }}', '{{ value is test }}',
                       '{{ value is filter(env=none) }}', '{{ value is test(value="none") }}'):
            def observe(native):
                environment = jinja2.Environment()
                environment.filters['application'] = lambda x: x
                environment.tests['application'] = lambda x: True
                try: return render(source, {'value': 'application'}, native, environment)
                except Exception as error: return type(error).__name__, str(error)
            with self.subTest(source=source): self.assertEqual(observe(True), observe(False))

    def test_errors_and_keyword_binding(self):
        for expression, values in [
            ('value|int(base=16)', {'value': 'ff'}),
            ('value|int(value=2)', {'value': '3'}),
            ('value|trim(unknown=4)', {'value': '3'}),
            ('value|upper(1, 2)', {'value': '3'}),
            ('value|round(method="bad")', {'value': 3.14}),
            ('value|int', {'value': float('inf')}),
            ('value|float', {'value': None}),
        ]:
            def observe(native):
                try: return ('value', render('{{ ' + expression + ' }}', values, native))
                except Exception as error: return ('error', type(error).__name__, str(error))
            with self.subTest(expression=expression): self.assertEqual(observe(True), observe(False))

    def test_mutable_defaults_and_code_remain_user_protocols(self):
        function = filters.do_int
        defaults = function.__defaults__
        try:
            for new_defaults in [(23, 10), (0, 16), (), (8, 9, 10, 11)]:
                function.__defaults__ = new_defaults
                for expression in ('value|int', 'value|int(7)', 'value|int(7, 10, 2)'):
                    def observe(native):
                        try: return ('value', render('{{ ' + expression + ' }}', {'value': 'bad'}, native))
                        except Exception as error: return ('error', type(error).__name__, str(error))
                    with self.subTest(defaults=new_defaults, expression=expression):
                        self.assertEqual(observe(True), observe(False))
        finally: function.__defaults__ = defaults
        old_code = function.__code__
        def custom(value, default=0, base=10): return 'custom'
        try:
            function.__code__ = custom.__code__
            self.assertEqual(self.check('{{ value|int }}', {'value': '7'}), 'custom')
        finally: function.__code__ = old_code

    def test_foreign_protocols_and_exception_state(self):
        def observe(native):
            events = []
            class Value:
                def __int__(self):
                    events.append(('int', type(sys.exception()).__name__))
                    raise ValueError('first attempt')
                def __float__(self):
                    events.append(('float', type(sys.exception()).__name__))
                    return 12.7
            output = render('{{ value|int }}', {'value': Value()}, native)
            return output, events
        self.assertEqual(observe(True), observe(False))
        self.check('{{ value|upper }}', {'value': Markup('<b>safe</b>')})

    def test_round_preserves_replaced_cast_helper(self):
        original = filters.t
        try:
            filters.t = SimpleNamespace(cast=lambda kind, value: value + 10)
            self.assertEqual(self.check('{{ value|round(0,"floor") }}', {'value': 2.7}), '12.0')
            self.assertEqual(self.check('{{ 2.7|round(0,"floor") }}'), '12.0')
        finally:
            filters.t = original

    def test_user_filter_effect_order_and_failed_constants(self):
        def observe(native):
            events = []
            environment = jinja2.Environment()
            def mark(value): events.append(value); return value
            environment.filters['mark'] = mark
            result = render('{{ ("subject"|mark)|default("arg"|mark) }}', {}, native, environment)
            return result, events
        self.assertEqual(observe(True), observe(False))

    def test_complete_docx_equivalence(self):
        source = '{{ value|upper }} {{ " 4.9 "|trim|int }} {{ number|round(1,"floor") }} {{ value|reverse|join }} {{ items|last }}'
        values = {'value': 'façade', 'number': 2.34, 'items': [1, 2, 3]}
        reference = generate(docxtpl, source, values)
        with no_standard_python(): candidate = generate(docxtpl_native, source, values)
        comparison = compare_docx(reference, candidate)
        self.assertTrue(comparison['equivalent'], comparison)

    def test_extension_ast_method_override_is_respected(self):
        original = nodes.Const.as_const
        try:
            nodes.Const.as_const = lambda self, eval_ctx=None: 'extension'
            self.assertEqual(docxtpl_native._extension().constant_value(nodes.Const(1), nodes.EvalContext(jinja2.Environment())), 'extension')
        finally: nodes.Const.as_const = original

    def test_constant_membership_overrides_and_code_mutation(self):
        originals = dict(nodes._cmpop_to_func)
        source = '{{ 1 in [1] }} {{ 1 not in [1] }}'
        try:
            nodes._cmpop_to_func['in'] = lambda left, right: 'custom-in'
            nodes._cmpop_to_func['notin'] = lambda left, right: 'custom-notin'
            self.assertEqual(self.check(source), 'custom-in custom-notin')
            nodes._cmpop_to_func.update(originals)
            function = nodes._cmpop_to_func['in']
            code = function.__code__
            try:
                function.__code__ = (lambda left, right: 'changed-code').__code__
                self.assertEqual(self.check('{{ 1 in [1] }}'), 'changed-code')
            finally:
                function.__code__ = code
        finally:
            nodes._cmpop_to_func.clear()
            nodes._cmpop_to_func.update(originals)

    def test_recursive_ast_is_bounded(self):
        node = nodes.Neg(nodes.Const(1))
        node.node = node
        with self.assertRaises(RecursionError):
            docxtpl_native._extension().constant_value(node, nodes.EvalContext(jinja2.Environment()))
