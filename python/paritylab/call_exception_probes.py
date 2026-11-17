"""Observe native callback handlers in complete sync and async documents."""
from functools import partial
import io
import sys
from unittest.mock import patch

from docx import Document
import jinja2
import jinja2.runtime as runtime
from .protocol_base import Probe, Result, generate


def execute(engine, *, asynchronous):
    observations = {}
    outputs = {}
    for label in ('stop', 'rebound', 'undefined_failure', 'invalid_handler'):
        failure = ValueError('callback') if label == 'rebound' else StopIteration('callback')
        undefined_failure = ValueError('undefined failed')
        trace = []
        class Undefined(jinja2.Undefined):
            def __init__(self, *args, **kwargs):
                trace.append(sys.exception() is failure)
                if label == 'undefined_failure':
                    raise undefined_failure
                super().__init__(*args, **kwargs)
        def callback():
            if label == 'rebound':
                runtime.StopIteration = ValueError
            elif label == 'invalid_handler':
                runtime.StopIteration = object()
            raise failure
        environment = jinja2.Environment(undefined=Undefined, enable_async=asynchronous)
        with patch.object(runtime, 'StopIteration', StopIteration, create=True):
            try:
                blob = generate(engine, 'before {{ callback() }} after', {'callback': callback}, environment)
            except Exception as error:
                observations[label] = {
                    'type': type(error).__name__, 'message': str(error),
                    'same_instance': error is undefined_failure,
                    'context_is_callback': error.__context__ is failure, 'trace': trace,
                }
            else:
                observations[label] = {'text': Document(io.BytesIO(blob)).paragraphs[0].text, 'trace': trace}
                outputs[label + '.docx'] = blob
    return Result(observations, outputs)


EXPECTED = {
    'stop': {'text': 'before  after', 'trace': [True]},
    'rebound': {'text': 'before  after', 'trace': [True]},
    'undefined_failure': {'type': 'ValueError', 'message': 'undefined failed',
                          'same_instance': True, 'context_is_callback': True, 'trace': [True]},
    'invalid_handler': {'type': 'TypeError',
                        'message': 'catching classes that do not inherit from BaseException is not allowed',
                        'same_instance': False, 'context_is_callback': True, 'trace': []},
}

PROBES = {
    'callback_exception_handler_sync': Probe(partial(execute, asynchronous=False), EXPECTED),
    'callback_exception_handler_async': Probe(partial(execute, asynchronous=True), EXPECTED),
}
