"""Intercepted arithmetic and unary/postfix precedence in complete DOCX files."""
import jinja2
from jinja2.sandbox import SandboxedEnvironment
from .protocol_base import Probe, generate, observed


def _operators(engine, asynchronous):
    environment = SandboxedEnvironment(enable_async=asynchronous)
    environment.intercepted_binops = frozenset({'+', '-', '*', '/', '//', '%', '**'})
    environment.intercepted_unops = frozenset({'+', '-'})
    trace = []
    def wrap(token, operation):
        def callback(*args):
            trace.append([token, *args])
            return operation(*args)
        return callback
    for token in environment.intercepted_binops:
        environment.binop_table[token] = wrap(token, environment.binop_table[token])
    for token in environment.intercepted_unops:
        environment.unop_table[token] = wrap('unary '+token, environment.unop_table[token])
    def left():
        trace.append('left')
        return 7
    source = '|'.join('{{ left() '+token+' right }}' for token in ('+', '-', '*', '/', '//', '%', '**'))
    source += '|{{ +left() }}|{{ -left() }}'
    return observed(generate(engine, source, {'left': left, 'right': 2}, environment), trace)


def sandbox_operators_sync(engine):
    return _operators(engine, False)


def sandbox_operators_async(engine):
    return _operators(engine, True)


def unary_postfix_precedence(engine):
    values = {'value': lambda: 7, 'obj': {'amount': 7}, 'items': [7]}
    source = '{{ +value() }}|{{ -value() }}|{{ -obj.amount }}|{{ -items[0] }}|{{ --value() }}|{{ -value()|abs }}|{{ -items[0] ** 2 }}'
    return observed(generate(engine, source, values, jinja2.Environment()))


_expected = {'text': '9|5|14|3.5|3|1|49|7|-7', 'trace': [
    item for token in ('+', '-', '*', '/', '//', '%', '**') for item in ('left', [token, 7, 2])
] + ['left', ['unary +', 7], 'left', ['unary -', 7]]}

PROBES = {
    'sandbox_operators_sync': Probe(sandbox_operators_sync, _expected),
    'sandbox_operators_async': Probe(sandbox_operators_async, _expected),
    'unary_postfix_precedence': Probe(unary_postfix_precedence, {'text': '7|-7|-7|-7|7|7|49'}),
}
