"""Unpacked keyword objects and errors through complete native documents."""
from functools import partial
import jinja2
import sys
from jinja2.sandbox import SandboxedEnvironment
from .protocol_base import Probe, Result, generate, observed

VARIANTS = ('plain', 'key_protocols', 'surrogate_key', 'mapping', 'dict_subclass',
            'mapping_error', 'nonmapping', 'nonstring_key', 'duplicate', 'static_and_dynamic')


def protocol_key(name, trace):
    class Key(str):
        def __hash__(self):
            trace.append(['hash', str(self)])
            return str.__hash__(self)
        def __eq__(self, other):
            trace.append(['equal', str(other)])
            return str.__eq__(self, other)
    return Key(name)


def observe(engine, asynchronous, sandboxed, variant):
    trace = []
    failure = ValueError('mapping failed')
    key = protocol_key('label', trace) if variant == 'key_protocols' else 'label'
    if variant == 'surrogate_key': key = '\ud800'
    kwargs = {key: 'done'}
    class Mapping:
        def keys(self):
            trace.append('keys')
            return ['label']
        def __getitem__(self, name):
            trace.append(['getitem', name])
            if variant == 'mapping_error': raise failure
            return 'done'
    class Dict(dict):
        def __iter__(self):
            trace.append('iter')
            return super().__iter__()
        def keys(self):
            trace.append('keys')
            return super().keys()
        def __getitem__(self, name):
            trace.append(['getitem', name])
            return super().__getitem__(name)
    if variant in ('mapping', 'mapping_error'): kwargs = Mapping()
    if variant == 'dict_subclass': kwargs = Dict(kwargs)
    if variant == 'nonmapping': kwargs = 42
    if variant == 'nonstring_key': kwargs = {42: 'done'}
    def callback(value, **keywords):
        trace.append(['callback', value, next(iter(keywords)) is key])
        return str(value + 1) + '/' + keywords[key]
    source = '{{ callback(value, **kwargs) }}'
    if variant == 'static_and_dynamic': source = '{{ callback(value, extra=7, **kwargs) }}'
    if variant == 'duplicate': source = '{{ callback(value, label="first", **kwargs) }}'
    environment = (SandboxedEnvironment if sandboxed else jinja2.Environment)(enable_async=asynchronous)
    trace.clear()
    try:
        blob = generate(engine, source, {'callback': callback, 'value': 41, 'kwargs': kwargs}, environment)
    except BaseException as error:
        return {'trace': trace, 'error': [type(error).__name__, str(error), error is failure]}, None
    return {'trace': trace, 'error': None}, blob



def execute(engine, *, asynchronous, sandboxed):
    observations, outputs = {}, {}
    for variant in VARIANTS:
        observation, blob = observe(engine, asynchronous, sandboxed, variant)
        observations[variant] = observation
        if blob is not None: outputs[variant + '.docx'] = blob
    return Result(observations, outputs)


# Fixed reference observations, shared by Python 3.11/3.12 and sync/async.
EXPECTED = {False: {'plain': {'trace': [['callback', 41, True]], 'error': None},
         'key_protocols': {'trace': [['equal', '_Context__self'],
                                     ['equal', '_Context__obj'],
                                     ['hash', 'label'],
                                     ['equal', 'value'],
                                     ['hash', 'label'],
                                     ['callback', 41, True],
                                     ['hash', 'label']],
                           'error': None},
         'surrogate_key': {'trace': [['callback', 41, True]], 'error': None},
         'mapping': {'trace': ['keys', ['getitem', 'label'], ['callback', 41, True]], 'error': None},
         'dict_subclass': {'trace': ['keys', ['getitem', 'label'], ['callback', 41, True]], 'error': None},
         'mapping_error': {'trace': ['keys', ['getitem', 'label']],
                           'error': ['ValueError', 'mapping failed', True]},
         'nonmapping': {'trace': [],
                        'error': ['TypeError',
                                  'jinja2.runtime.Context.call() argument after ** must be a mapping, not '
                                  'int',
                                  False]},
         'nonstring_key': {'trace': [], 'error': ['TypeError', 'keywords must be strings', False]},
         'duplicate': {'trace': [],
                       'error': ['TypeError',
                                 'jinja2.runtime.Context.call() got multiple values for keyword argument '
                                 "'label'",
                                 False]},
         'static_and_dynamic': {'trace': [['callback', 41, False]], 'error': None}},
 True: {'plain': {'trace': [['callback', 41, True]], 'error': None},
        'key_protocols': {'trace': [['equal', '_SandboxedEnvironment__self'],
                                    ['equal', '_SandboxedEnvironment__context'],
                                    ['equal', '_SandboxedEnvironment__obj'],
                                    ['hash', 'label'],
                                    ['equal', '_Context__self'],
                                    ['equal', '_Context__obj'],
                                    ['hash', 'label'],
                                    ['equal', 'value'],
                                    ['hash', 'label'],
                                    ['callback', 41, True],
                                    ['hash', 'label']],
                          'error': None},
        'surrogate_key': {'trace': [['callback', 41, True]], 'error': None},
        'mapping': {'trace': ['keys', ['getitem', 'label'], ['callback', 41, True]], 'error': None},
        'dict_subclass': {'trace': ['keys', ['getitem', 'label'], ['callback', 41, True]], 'error': None},
        'mapping_error': {'trace': ['keys', ['getitem', 'label']],
                          'error': ['ValueError', 'mapping failed', True]},
        'nonmapping': {'trace': [],
                       'error': ['TypeError',
                                 'jinja2.sandbox.SandboxedEnvironment.call() argument after ** must be a '
                                 'mapping, not int',
                                 False]},
        'nonstring_key': {'trace': [], 'error': ['TypeError', 'keywords must be strings', False]},
        'duplicate': {'trace': [],
                      'error': ['TypeError',
                                'jinja2.sandbox.SandboxedEnvironment.call() got multiple values for keyword '
                                "argument 'label'",
                                False]},
        'static_and_dynamic': {'trace': [['callback', 41, False]], 'error': None}}}

PROBES = {
    "dynamic_keywords_" + ("sandbox_" if sandboxed else "") + ("async" if asynchronous else "sync"):
        Probe(partial(execute, asynchronous=asynchronous, sandboxed=sandboxed), EXPECTED[sandboxed])
    for sandboxed in (False, True) for asynchronous in (False, True)
}


def callee_probe(engine, *, kind):
    trace = []
    key = protocol_key('other' if kind == 'predicate' else 'label', trace)
    value = object()
    kwargs = {key: value if kind == 'predicate' else 'done'}
    def callback(value, **kwargs):
        trace.append(['callback', next(iter(kwargs)) is key])
        return 'done'
    environment = jinja2.Environment()
    environment.filters['custom'] = callback
    environment.tests['custom'] = callback
    sources = {
        'filter': '{{ value|custom(**kwargs) }}',
        'test': '{% if value is custom(**kwargs) %}yes{% endif %}',
        'predicate': '{{ value is sameas(**kwargs) }}',
        'macro': '{% macro m(value) %}{{ custom(value, **kwargs) }}{% endmacro %}{{ m(value, **kwargs) }}',
    }
    trace.clear()
    blob = generate(engine, sources[kind], {'kwargs': kwargs, 'custom': callback, 'value': value}, environment)
    return observed(blob, trace)


CALLEE_EXPECTED = {
    'filter': {'text': 'done', 'trace': [['equal', 'value'], ['hash', 'label'], ['callback', True]]},
    'test': {'text': 'yes', 'trace': [['equal', 'value'], ['hash', 'label'], ['callback', True]]},
    'predicate': {'text': 'True', 'trace': [['equal', 'value'], ['equal', 'other']]},
    'macro': {'text': 'done', 'trace': [
        ['equal', '_Context__self'], ['equal', '_Context__obj'], ['hash', 'label'],
        ['equal', 'self'], ['hash', 'label'], ['equal', '_Context__self'],
        ['equal', '_Context__obj'], ['hash', 'label'], ['equal', 'value'],
        ['hash', 'label'], ['callback', True],
    ]},
}
PROBES.update({'dynamic_keywords_' + kind: Probe(partial(callee_probe, kind=kind), expected)
               for kind, expected in CALLEE_EXPECTED.items()})


def key_error_probe(engine):
    observations, outputs = {}, {}
    for kind in ('ok', 'duplicate', 'key_error', 'subclass', 'args_hook', 'empty', 'many'):
        trace = []
        class Failure(KeyError):
            def __getattribute__(self, name):
                if kind == 'args_hook' and name == 'args':
                    trace.append('args hook')
                    raise AssertionError('exception attribute hook executed')
                return super().__getattribute__(name)
        failure = (Failure('label') if kind in ('subclass', 'args_hook') else
                   KeyError() if kind == 'empty' else KeyError('a', 'b') if kind == 'many' else KeyError('label'))
        class Mapping:
            def keys(self): return ['label']
            def __getitem__(self, key): raise failure
        kwargs = {'label': 'done'} if kind in ('ok', 'duplicate') else Mapping()
        source = '{{ callback(label="first", **kwargs) }}' if kind == 'duplicate' else '{{ callback(**kwargs) }}'
        try:
            blob = generate(engine, source, {'callback': lambda **kwargs: kwargs['label'], 'kwargs': kwargs}, jinja2.Environment())
        except BaseException as error:
            observations[kind] = {'type': type(error).__name__, 'message': str(error),
                                  'same_instance': error is failure, 'context': error.__context__ is None, 'trace': trace}
        else:
            observations[kind] = observed(blob, trace).observation
            outputs[kind + '.docx'] = blob
    return Result(observations, outputs)


_DUPLICATE = {'type': 'TypeError', 'message': "jinja2.runtime.Context.call() got multiple values for keyword argument 'label'",
              'same_instance': False, 'context': True, 'trace': []}
_KEY_ERROR_EXPECTED = {
    'ok': {'text': 'done', 'trace': []}, 'duplicate': _DUPLICATE,
    'empty': {'type': 'KeyError', 'message': '', 'same_instance': True, 'context': True, 'trace': []},
    'many': {'type': 'KeyError', 'message': "('a', 'b')", 'same_instance': True, 'context': True, 'trace': []},
}
for _kind in ('key_error', 'subclass', 'args_hook'):
    _KEY_ERROR_EXPECTED[_kind] = (_DUPLICATE if sys.version_info >= (3, 12) else {
        'type': 'KeyError' if _kind == 'key_error' else 'Failure', 'message': "'label'",
        'same_instance': True, 'context': True, 'trace': [],
    })
PROBES['dynamic_keywords_key_error'] = Probe(key_error_probe, _KEY_ERROR_EXPECTED)
