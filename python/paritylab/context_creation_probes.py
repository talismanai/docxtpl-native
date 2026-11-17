"""Context construction precedes root execution, including its failure boundary."""
import io
from functools import partial
from unittest.mock import patch
import jinja2
from jinja2.runtime import Context
from .protocol_base import Probe, Result, template


def context_creation(engine, *, asynchronous):
    observations = []
    output_blob = None
    for kind in ('count', 'StopIteration', 'ValueError'):
        trace = []
        original = Context.__init__
        failure = StopIteration('context init failed') if kind == 'StopIteration' else ValueError('context init failed')
        def initialize(self, *args, **kwargs):
            trace.append('init')
            if kind != 'count':
                raise failure
            original(self, *args, **kwargs)
        document = engine.DocxTemplate(io.BytesIO(template('{{ value }}')))
        environment = jinja2.Environment(enable_async=asynchronous)
        try:
            with patch.object(Context, '__init__', initialize):
                document.render({'value': 'X'}, jinja_env=environment)
            output = io.BytesIO()
            document.save(output)
            if kind == 'count':
                output_blob = output.getvalue()
            observation = {'kind': kind, 'success': True, 'initializations': len(trace)}
        except BaseException as error:
            observation = {'kind': kind, 'error': type(error).__name__, 'message': str(error),
                           'original': error is failure, 'cause': error.__cause__ is failure,
                           'initializations': len(trace)}
        failure.__traceback__ = None
        observations.append(observation)
    return Result({'observations': observations}, {'document.docx': output_blob} if output_blob else {})


PROBES = {}
for asynchronous in (False, True):
    suffix = 'async' if asynchronous else 'sync'
    PROBES['context_initialization_' + suffix] = Probe(
        partial(context_creation, asynchronous=asynchronous),
        {'observations': [
            {'kind': 'count', 'success': True, 'initializations': 7},
            {'kind': 'StopIteration', 'error': 'RuntimeError' if asynchronous else 'StopIteration',
             'message': 'coroutine raised StopIteration' if asynchronous else 'context init failed',
             'original': not asynchronous, 'cause': asynchronous, 'initializations': 1},
            {'kind': 'ValueError', 'error': 'ValueError', 'message': 'context init failed',
             'original': True, 'cause': False, 'initializations': 1},
        ]})
