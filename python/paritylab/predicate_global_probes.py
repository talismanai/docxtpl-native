"""Live globals and return-object protocols of the native predicate family."""
from contextlib import ExitStack
from unittest.mock import patch
import jinja2.tests as predicates
from .protocol_base import Probe, Result, generate, observed

NAMES = ('defined', 'undefined', 'integer', 'float', 'string')
CLASSES = {'defined': 'Undefined', 'undefined': 'Undefined', 'integer': 'int',
           'float': 'float', 'string': 'str'}


def execute(engine):
    observations, outputs = {}, {}
    for truth in (False, True):
        trace = []
        aliases = {name: object() for name in set(CLASSES.values())}
        class Verdict:
            def __bool__(self):
                trace.append('truth')
                return truth
        verdict = Verdict()
        def check(value, kind):
            trace.append(['check', next(name for name, alias in aliases.items() if kind is alias)])
            return verdict
        def capture(name, result):
            label = 'marker' if result is verdict else str(result)
            trace.append(['result', name, label])
            return label
        source = '|'.join('{{ capture("'+name+'", value is '+name+') }}' for name in NAMES)
        with ExitStack() as stack:
            stack.enter_context(patch.object(predicates, 'isinstance', check, create=True))
            for name, alias in aliases.items():
                stack.enter_context(patch.object(predicates, name, alias, create=True))
            blob = generate(engine, source, {'value': 41, 'capture': capture})
        label = str(truth)
        observations[label] = observed(blob, trace).observation
        outputs[label + '.docx'] = blob
    return Result(observations, outputs)


EXPECTED = {}
for truth in (False, True):
    results = [str(not truth), 'marker', 'True' if truth else 'marker', 'marker', 'marker']
    trace = []
    for name, result in zip(NAMES, results):
        trace.append(['check', CLASSES[name]])
        if name in ('defined', 'integer'): trace.append('truth')
        trace.append(['result', name, result])
    EXPECTED[str(truth)] = {'text': '|'.join(results), 'trace': trace}
PROBES = {'native_predicate_globals': Probe(execute, EXPECTED)}
