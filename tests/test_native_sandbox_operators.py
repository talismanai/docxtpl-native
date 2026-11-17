"""Sandbox arithmetic selection and dispatch use the existing native VM."""
import sys
import unittest
from contextlib import ExitStack, contextmanager

import docxtpl_native
import jinja2
from jinja2.sandbox import SandboxedEnvironment
from docxtpl_native.bridge import BridgeConfig
from native_audit import forbid
from paritylab.protocol_base import generate, observed


@contextmanager
def native_operators_only():
    forbidden = {SandboxedEnvironment.call_binop.__code__, SandboxedEnvironment.call_unop.__code__}
    previous = sys.getprofile()
    def profile(frame, event, argument):
        if event == 'call' and frame.f_code in forbidden:
            raise AssertionError('Python sandbox operator algorithm executed')
    sys.setprofile(profile)
    try:
        yield
    finally:
        sys.setprofile(previous)


def render(native, environment, source, values, gate=True):
    if not native:
        return environment.from_string(source).render(values)
    with native_operators_only() if gate else ExitStack():
        return docxtpl_native._extension().PythonRenderSession(
            BridgeConfig(environment, False), values).render('case', source)


class NativeSandboxOperatorTests(unittest.TestCase):
    def environment(self, asynchronous=False):
        return operators_environment(asynchronous)

    def test_all_standard_operators_sync_and_async(self):
        sources = ['{{ left '+token+' right }}' for token in ('+', '-', '*', '/', '//', '%', '**')]
        sources += ['{{ +left }}', '{{ -left }}']
        for asynchronous in (False, True):
            for source in sources:
                with self.subTest(asynchronous=asynchronous, source=source):
                    values = {'left': 7, 'right': 2}
                    self.assertEqual(render(True, self.environment(asynchronous), source, values),
                                     render(False, self.environment(asynchronous), source, values))

    def test_unary_postfix_precedence_without_interception(self):
        values = {'value': lambda: 7, 'obj': {'amount': 7}, 'items': [7]}
        for source in ('{{ +value() }}', '{{ -value() }}', '{{ -obj.amount }}',
                       '{{ -items[0] }}', '{{ --value() }}', '{{ -value()|abs }}',
                       '{{ -items[0] ** 2 }}', '{{ 7 is equalto (+value()) }}'):
            with self.subTest(source=source):
                self.assertEqual(render(True, jinja2.Environment(), source, values),
                                 render(False, jinja2.Environment(), source, values))

    def test_sign_after_bare_test_is_a_binary_operator(self):
        for token in ('+', '-'):
            def run(native):
                environment = jinja2.Environment()
                environment.tests['pick'] = lambda value: 10
                return render(native, environment, '{{ value is pick '+token+' right }}', {'value': 7, 'right': 3})
            self.assertEqual(run(True), run(False))

    def test_operator_token_identity_within_compiled_template(self):
        def run(native):
            environment = self.environment()
            tokens = []
            def callback(context, token, left, right):
                tokens.append(token)
                return 42
            environment.call_binop = callback
            output = render(native, environment, '{% for x in values %}{{ left ** right }}{{ left ** right }}{% endfor %}',
                            {'left': 2, 'right': 3, 'values': [1, 2]})
            return output, [token is tokens[0] for token in tokens]
        self.assertEqual(run(True), run(False))

    def test_method_is_resolved_before_operands_and_table_after_operands(self):
        for unary in (False, True):
            def run(native):
                trace = []
                method_name = 'call_unop' if unary else 'call_binop'
                class Environment(SandboxedEnvironment):
                    def __getattribute__(self, name):
                        if name == method_name:
                            trace.append('method')
                        return super().__getattribute__(name)
                environment = Environment()
                environment.intercepted_binops = frozenset({'+'})
                environment.intercepted_unops = frozenset({'+'})
                table = environment.unop_table if unary else environment.binop_table
                def selected(*args): trace.append(['table callback', *args]); return 42
                def left():
                    trace.append('left')
                    setattr(environment, method_name, lambda *args: 'wrong method')
                    table['+'] = selected
                    return 5
                def right(): trace.append('right'); return 8
                source = '{{ +left() }}' if unary else '{{ left() + right() }}'
                result = render(native, environment, source, {'left': left, 'right': right})
                return result, trace
            with self.subTest(unary=unary):
                self.assertEqual(run(True), run(False))

    def test_runtime_membership_mutations_do_not_rewrite_compiled_choices(self):
        for initially_intercepted in (False, True):
            def run(native):
                environment = SandboxedEnvironment()
                environment.intercepted_binops = {'+'} if initially_intercepted else set()
                environment.binop_table['+'] = lambda left, right: 99
                def change():
                    environment.intercepted_binops = set() if initially_intercepted else {'+'}
                    return ''
                return render(native, environment, '{{ change() }}{{ left + right }}',
                              {'change': change, 'left': 2, 'right': 3})
            self.assertEqual(run(True), run(False))

    def test_constant_intercepted_operands_execute_after_runtime_callbacks(self):
        def run(native):
            trace = []
            environment = self.environment()
            def add(left, right): trace.append('operator'); return left + right
            def touch(): trace.append('touch'); return ''
            environment.binop_table['+'] = add
            return render(native, environment, '{{ touch() }}{{ 2 + 3 }}', {'touch': touch}), trace
        self.assertEqual(run(True), run(False))
        self.assertEqual(run(True), ('5', ['touch', 'operator']))

    def test_method_failure_happens_before_operand_callbacks(self):
        failure = ValueError('method lookup')
        for native in (False, True):
            trace = []
            class Environment(SandboxedEnvironment):
                @property
                def call_binop(self): raise failure
            environment = Environment()
            environment.intercepted_binops = frozenset({'+'})
            def operand(): trace.append('operand'); return 2
            with self.assertRaises(ValueError) as caught:
                render(native, environment, '{{ operand() + operand() }}', {'operand': operand})
            self.assertIs(caught.exception, failure)
            self.assertEqual(trace, [])

    def test_table_failure_follows_operand_callbacks_and_retains_identity(self):
        failure = KeyError('table failed')
        for native in (False, True):
            trace = []
            class Table:
                def __getitem__(self, key): trace.append(('table', key)); raise failure
            environment = self.environment()
            environment.binop_table = Table()
            def operand(): trace.append('operand'); return 2
            with self.assertRaises(KeyError) as caught:
                render(native, environment, '{{ operand() + operand() }}', {'operand': operand})
            self.assertIs(caught.exception, failure)
            self.assertEqual(trace, ['operand', 'operand', ('table', '+')])

    def test_awaitable_operator_result_is_not_implicitly_awaited(self):
        for asynchronous in (False, True):
            def run(native):
                trace = []
                class Result:
                    def __await__(self):
                        trace.append('unexpected await')
                        raise AssertionError('operator result awaited')
                    def __str__(self): trace.append('string'); return 'result'
                environment = self.environment(asynchronous)
                environment.binop_table['+'] = lambda left, right: Result()
                return render(native, environment, '{{ left + right }}', {'left': 2, 'right': 3}), trace
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(run(True), run(False))

    def test_rebound_standard_method_uses_its_receiver_table(self):
        for unary in (False, True):
            donor = self.environment()
            donor.binop_table['+'] = lambda *args: 'donor'
            donor.unop_table['+'] = lambda *args: 'donor'
            environment = self.environment()
            name = 'call_unop' if unary else 'call_binop'
            setattr(environment, name, getattr(donor, name))
            source = '{{ +left }}' if unary else '{{ left + right }}'
            self.assertEqual(render(True, environment, source, {'left': 2, 'right': 3}), 'donor')

    def test_custom_method_gets_context_and_current_arguments(self):
        def run(native):
            trace = []
            environment = self.environment()
            def call(context, operator, left, right):
                trace.append([context.environment is environment, context['answer'], operator, left, right])
                return 42
            environment.call_binop = call
            source = '{% set answer=7 %}{{ left + right }}'
            return render(native, environment, source, {'left': 2, 'right': 3}), trace
        self.assertEqual(run(True), run(False))

    def test_standard_method_code_changed_by_operand_is_honored(self):
        function = SandboxedEnvironment.call_binop
        original = function.__code__
        def replacement(self, context, operator, left, right): return 'new code'
        def run(native):
            environment = self.environment()
            def change(): function.__code__ = replacement.__code__; return 2
            try:
                return render(native, environment, '{{ change() + right }}', {'change': change, 'right': 3}, gate=False)
            finally:
                function.__code__ = original
        self.assertEqual(run(True), run(False))
        self.assertEqual(run(True), 'new code')

    def test_nested_operators_macros_blocks_and_includes(self):
        def run(native):
            environment = self.environment()
            environment.loader = jinja2.DictLoader({'child': '{{ left * right }}'})
            source = '{% macro calculate(x) %}{{ -(x + right) }}{% endmacro %}{% block body %}{{ calculate(left) }}|{% include "child" %}{% endblock %}'
            return render(native, environment, source, {'left': 3, 'right': 2})
        self.assertEqual(run(True), run(False))

    def test_extension_generated_ast_uses_the_same_interception(self):
        from jinja2 import nodes
        from jinja2.ext import Extension
        class Add(Extension):
            tags = {'addition'}
            def parse(self, parser):
                token = next(parser.stream)
                return nodes.Output([nodes.Add(nodes.Name('left', 'load'), nodes.Name('right', 'load'))]).set_lineno(token.lineno)
        def run(native):
            environment = SandboxedEnvironment(extensions=[Add])
            environment.intercepted_binops = frozenset({'+'})
            environment.binop_table['+'] = lambda left, right: 42
            return render(native, environment, '{% addition %}', {'left': 2, 'right': 3})
        self.assertEqual(run(True), run(False))
        self.assertEqual(run(True), '42')

    def test_complete_docx_standard_operators_run_in_rust(self):
        # Document equivalence with docxtpl: test_audited_documents_reference.
        for asynchronous in (False, True):
            with native_operators_only(), forbid('jinja2.Template.render', message='Python renderer called'):
                native = operators_document(docxtpl_native, asynchronous)
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observed(native).observation['text'], '9 / -7 / 49')


def operators_environment(asynchronous=False):
    environment = SandboxedEnvironment(enable_async=asynchronous)
    environment.intercepted_binops = frozenset({'+', '-', '*', '/', '//', '%', '**'})
    environment.intercepted_unops = frozenset({'+', '-'})
    return environment


def operators_document(engine, asynchronous):
    """A complete document with intercepted sandbox operators."""
    source = '{{ left + right }} / {{ -left }} / {{ left ** right }}'
    return generate(engine, source, {'left': 7, 'right': 2}, operators_environment(asynchronous))


if __name__ == '__main__':
    unittest.main()
