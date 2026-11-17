"""Concatenation protocols, shared by the corpus and native-execution tests."""
from functools import partial

import jinja2
from markupsafe import Markup

from .protocol_base import Probe, Result, generate, observed


def make_case(shape, autoescape):
    trace = []
    failure = ValueError('html failure')

    class Text(str):
        def __str__(self):
            trace.append('subclass-str')
            return str.__str__(self)

        def __getattribute__(self, name):
            if name == '__html__':
                trace.append('lookup-html')
            return super().__getattribute__(name)

    class Safe(Text):
        def __html__(self):
            trace.append('html')
            if shape == 'error':
                raise failure
            return '<safe>'

    class Object:
        def __str__(self):
            trace.append('str')
            return '<object>'

        def __html__(self):
            trace.append('object-html')
            return '<unused>'

    values = {
        'plain': ('<a>', Object(), Text('<b>')),
        'markup': (Object(), Markup('<b>'), Text('<c>')),
        'html': (Text('<a>'), Safe('<b>'), Object()),
        'error': (Text('<a>'), Safe('<b>'), Object()),
        'surrogate': ('\ud800', Markup('<b>'), 'α'),
        'subclass': (Text('<a>'), Text('<b>'), Object()),
    }[shape]

    def consume(value):
        trace.append([type(value).__module__, type(value).__name__, repr(value)])
        return 'done'

    return (jinja2.Environment(autoescape=autoescape), '{{ consume(a ~ b ~ c) }}',
            dict(zip(('a', 'b', 'c'), values), consume=consume), trace, failure)


def execute(engine, *, shape, autoescape):
    environment, source, context, trace, failure = make_case(shape, autoescape)
    try:
        blob = generate(engine, source, context, environment)
    except ValueError as error:
        return Result({'exception': type(error).__name__, 'message': str(error),
                       'same_instance': error is failure, 'trace': trace}, {})
    return observed(blob, trace)


SCENARIOS = {
    'concat_' + label + ('_autoescape' if autoescape else ''): (shape, autoescape)
    for shape, label in (('plain', 'objects'), ('markup', 'markup_values'),
                         ('html', 'html_subclass'), ('error', 'html_error'),
                         ('surrogate', 'surrogate'), ('subclass', 'str_subclass'))
    for autoescape in (False, True)
}


EXPECTED = {'concat_objects': {'text': 'done',
                    'trace': ['str', 'subclass-str', ['builtins', 'str', "'<a><object><b>'"]]},
 'concat_objects_autoescape': {'text': 'done',
                               'trace': ['str',
                                         'lookup-html',
                                         ['builtins', 'str', "'<a><object><b>'"]]},
 'concat_markup_values': {'text': 'done',
                          'trace': ['str',
                                    'subclass-str',
                                    ['builtins', 'str', "'<object><b><c>'"]]},
 'concat_markup_values_autoescape': {'text': 'done',
                                     'trace': ['str',
                                               'lookup-html',
                                               'subclass-str',
                                               ['markupsafe',
                                                'Markup',
                                                "Markup('&lt;object&gt;<b>&lt;c&gt;')"]]},
 'concat_html_subclass': {'text': 'done',
                          'trace': ['subclass-str',
                                    'subclass-str',
                                    'str',
                                    ['builtins', 'str', "'<a><b><object>'"]]},
 'concat_html_subclass_autoescape': {'text': 'done',
                                     'trace': ['lookup-html',
                                               'lookup-html',
                                               'lookup-html',
                                               'subclass-str',
                                               'lookup-html',
                                               'lookup-html',
                                               'html',
                                               'str',
                                               ['markupsafe',
                                                'Markup',
                                                "Markup('&lt;a&gt;<safe>&lt;object&gt;')"]]},
 'concat_html_error': {'text': 'done',
                       'trace': ['subclass-str',
                                 'subclass-str',
                                 'str',
                                 ['builtins', 'str', "'<a><b><object>'"]]},
 'concat_html_error_autoescape': {'exception': 'ValueError',
                                  'message': 'html failure',
                                  'same_instance': True,
                                  'trace': ['lookup-html',
                                            'lookup-html',
                                            'lookup-html',
                                            'subclass-str',
                                            'lookup-html',
                                            'lookup-html',
                                            'html']},
 'concat_surrogate': {'text': 'done', 'trace': [['builtins', 'str', "'\\ud800<b>α'"]]},
 'concat_surrogate_autoescape': {'text': 'done',
                                 'trace': [['markupsafe', 'Markup', "Markup('\\ud800<b>α')"]]},
 'concat_str_subclass': {'text': 'done',
                         'trace': ['subclass-str',
                                   'subclass-str',
                                   'str',
                                   ['builtins', 'str', "'<a><b><object>'"]]},
 'concat_str_subclass_autoescape': {'text': 'done',
                                    'trace': ['lookup-html',
                                              'lookup-html',
                                              'str',
                                              ['builtins', 'str', "'<a><b><object>'"]]}}

PROBES = {name: Probe(partial(execute, shape=shape, autoescape=autoescape), EXPECTED[name],
                      assertion_only=shape == "error" and autoescape)
          for name, (shape, autoescape) in SCENARIOS.items()}
