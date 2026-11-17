"""Literal result oracle for the remaining context-free predicate algorithms."""
from decimal import Decimal
from markupsafe import Markup
from .protocol_base import Probe, generate, observed

NAMES = ('odd', 'even', 'divisibleby', 'lower', 'upper', 'mapping', 'number',
         'sequence', 'iterable', 'escaped', 'in')


def execute(engine):
    expressions = [
        'odd_value is odd', 'even_value is even', 'even_value is divisibleby(divisor)',
        'lower_value is lower', 'upper_value is upper', 'mapping_value is mapping',
        'number_value is number', 'sequence_value is sequence', 'sequence_value is iterable',
        'escaped_value is escaped', 'even_value is in(sequence_value)',
        'even_value is odd', 'odd_value is even', 'odd_value is divisibleby(divisor)',
        'upper_value is lower', 'lower_value is upper', 'even_value is mapping',
        'lower_value is number', 'even_value is sequence', 'even_value is iterable',
        'lower_value is escaped', 'odd_value is in(sequence_value)',
    ]
    values = {'odd_value': 5, 'even_value': 6, 'divisor': 3,
              'lower_value': 'straße', 'upper_value': 'Σ', 'mapping_value': {'a': 1},
              'number_value': Decimal('2.1'), 'sequence_value': [6], 'escaped_value': Markup('<b>')}
    return observed(generate(engine, '|'.join('{{ '+e+' }}' for e in expressions), values))


PROBES = {'native_predicate_extended': Probe(execute, {'text': '|'.join(['True']*11+['False']*11)})}
