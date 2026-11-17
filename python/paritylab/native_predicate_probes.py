"""Independent boundary and error oracles for predicates migrated to Rust."""
import jinja2
from .protocol_base import Probe, Result, generate, observed

NAMES = ('defined', 'undefined', 'none', 'boolean', 'false', 'true',
         'integer', 'float', 'string', 'sameas',
         'odd', 'even', 'divisibleby', 'lower', 'upper', 'mapping', 'number',
         'sequence', 'iterable', 'escaped', 'in')


def types(engine):
    class Integer(int): pass
    class Float(float): pass
    class String(str): pass
    class IntegerProxy:
        @property
        def __class__(self): return int
    class FloatProxy:
        @property
        def __class__(self): return float
    class StringProxy:
        @property
        def __class__(self): return str
    values = [None, False, True, 0, 10**100, Integer(3), 1.5, Float(2), '', String('x'), [], object()]
    values += [IntegerProxy(), FloatProxy(), StringProxy()]
    source = '{% for v in values %}{{ v is none }}{{ v is boolean }}{{ v is false }}{{ v is true }}{{ v is integer }}{{ v is float }}{{ v is string }};{% endfor %}'
    return observed(generate(engine, source, {'values': values}))


def identity(engine):
    marker = []; other = []
    return observed(generate(engine,
        '{{ value is sameas(value) }}|{{ value is sameas(other) }}|{{ value is sameas(other=value) }}',
        {'value': marker, 'other': other}))


def undefined(engine):
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
    return observed(generate(engine,
        '{{ missing is defined }}|{{ missing is undefined }}|{{ value is defined }}|{{ value is undefined }}|{{ missing is none }}',
        {'value': None}, environment))


def override(engine):
    trace = []
    environment = jinja2.Environment()
    def custom(value):
        trace.append(value)
        return 'custom'
    environment.tests['integer'] = custom
    return observed(generate(engine, '{{ value is integer }}', {'value': 'x'}, environment), trace)


def invalid(name, arguments, message):
    def execute(engine):
        try:
            generate(engine, '{{ value is '+name+'('+arguments+') }}', {'value': 1})
        except TypeError as error:
            return Result({'type': 'builtins.TypeError', 'message': str(error)}, {})
        raise AssertionError('expected TypeError')
    return Probe(execute, {'type': 'builtins.TypeError', 'message': message}, True)


PROBES = {
    'native_predicate_types': Probe(types, {'text':
        'TrueFalseFalseFalseFalseFalseFalse;'
        'FalseTrueTrueFalseFalseFalseFalse;'
        'FalseTrueFalseTrueFalseFalseFalse;'
        'FalseFalseFalseFalseTrueFalseFalse;'
        'FalseFalseFalseFalseTrueFalseFalse;'
        'FalseFalseFalseFalseTrueFalseFalse;'
        'FalseFalseFalseFalseFalseTrueFalse;'
        'FalseFalseFalseFalseFalseTrueFalse;'
        'FalseFalseFalseFalseFalseFalseTrue;'
        'FalseFalseFalseFalseFalseFalseTrue;'
        'FalseFalseFalseFalseFalseFalseFalse;'
        'FalseFalseFalseFalseFalseFalseFalse;'
        'FalseFalseFalseFalseTrueFalseFalse;'
        'FalseFalseFalseFalseFalseTrueFalse;'
        'FalseFalseFalseFalseFalseFalseTrue;'}),
    'native_predicate_identity': Probe(identity, {'text': 'True|False|True'}),
    'native_predicate_undefined': Probe(undefined, {'text': 'False|True|True|False|False'}),
    'native_predicate_override': Probe(override, {'text': 'custom', 'trace': ['x']}),
}
for name in NAMES:
    if name in ('sameas', 'divisibleby', 'in'):
        parameter = {'sameas': 'other', 'divisibleby': 'num', 'in': 'seq'}[name]
        variants = [('missing', '', f"test_{name}() missing 1 required positional argument: '{parameter}'"),
                    ('extra', 'value,value', f'test_{name}() takes 2 positional arguments but 3 were given'),
                    ('duplicate', 'value,value=value', f"test_{name}() got multiple values for argument 'value'"),
                    ('unknown', 'value,bogus=1', f"test_{name}() got an unexpected keyword argument 'bogus'")]
    else:
        variants = [('extra', 'value', f'test_{name}() takes 1 positional argument but 2 were given'),
                    ('duplicate', 'value=value', f"test_{name}() got multiple values for argument 'value'"),
                    ('unknown', 'bogus=1', f"test_{name}() got an unexpected keyword argument 'bogus'")]
    for label, arguments, message in variants:
        PROBES['native_predicate_'+name+'_'+label] = invalid(name, arguments, message)
