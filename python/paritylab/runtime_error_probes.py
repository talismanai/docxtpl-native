"""Escaped iteration errors are distinct from a renderer returning normally."""
from functools import partial
import io
import jinja2
from docx import Document
from .protocol_base import Probe, Result, generate


def make_case(asynchronous, origin, exception_type):
    failure = exception_type('stop marker')
    def callback(*args):
        raise failure
    class Value:
        def __str__(self):
            raise failure
    environment = jinja2.Environment(enable_async=asynchronous)
    if origin == 'filter':
        environment.filters['fail'] = callback
        source, values = '{{ value|fail }}', {'value': 'x'}
    elif origin == 'callback':
        source, values = '{{ callback() }}', {'callback': callback}
    else:
        source, values = '{{ value }}', {'value': Value()}
    return environment, source, values, failure


def exception_observation(error, failure):
    return {'status': 'error', 'type': type(error).__name__, 'message': str(error),
            'same_instance': error is failure, 'cause_same': error.__cause__ is failure,
            'context_same': error.__context__ is failure,
            'suppress_context': error.__suppress_context__}


def execute(engine, *, asynchronous):
    observations = {}
    outputs = {}
    for origin in ('filter', 'callback', 'str'):
        for exception_type in (StopIteration, StopAsyncIteration):
            label = origin + ':' + exception_type.__name__
            environment, source, values, failure = make_case(asynchronous, origin, exception_type)
            try:
                blob = generate(engine, source, values, environment)
            except BaseException as error:
                observations[label] = exception_observation(error, failure)
            else:
                observations[label] = {'status': 'ok', 'text': Document(io.BytesIO(blob)).paragraphs[0].text}
                outputs[label.replace(':', '-') + '.docx'] = blob
    return Result(observations, outputs)


def expected(asynchronous):
    result = {}
    for origin in ('filter', 'callback', 'str'):
        for name in ('StopIteration', 'StopAsyncIteration'):
            label = origin + ':' + name
            if origin == 'callback' and name == 'StopIteration':
                result[label] = {'status': 'ok', 'text': ''}
            elif asynchronous or name == 'StopIteration':
                result[label] = {'status': 'error', 'type': 'RuntimeError',
                                 'message': ('async generator' if asynchronous else 'generator') + ' raised ' + name,
                                 'same_instance': False, 'cause_same': True, 'context_same': True,
                                 'suppress_context': True}
            else:
                result[label] = {'status': 'error', 'type': name, 'message': 'stop marker',
                                 'same_instance': True, 'cause_same': False, 'context_same': False,
                                 'suppress_context': False}
    return result


PROBES = {
    'runtime_iteration_errors': Probe(partial(execute, asynchronous=False), expected(False)),
    'async_runtime_iteration_errors': Probe(partial(execute, asynchronous=True), expected(True)),
}
