"""Live callback policy dispatch through complete synchronous/asynchronous DOCX."""
from functools import partial
from unittest.mock import patch

import jinja2.runtime as runtime
from jinja2.sandbox import SandboxedEnvironment
from .protocol_base import Probe, generate, observed


def execute(engine, *, asynchronous):
    trace = []
    environment = SandboxedEnvironment(enable_async=asynchronous)

    def callback(*args, **kwargs):
        trace.append(['callback', args[0] is environment, list(kwargs)])
        return str(args[1] + 1) + '/' + kwargs['label']

    class Policy:
        context = object()
        eval_context = object()
        environment = object()

        @classmethod
        def from_obj(cls, obj):
            trace.append(['policy', 'callback' if obj is callback else '__call__'])
            return cls.environment if obj is callback else None

    with patch.object(runtime, '_PassArg', Policy):
        blob = generate(engine, '{{ callback(value, label="done") }}',
                        {'callback': callback, 'value': 41}, environment)
    return observed(blob, trace)


EXPECTED = {'text': '42/done', 'trace': [
    ['policy', '__call__'], ['policy', 'callback'], ['callback', True, ['label']],
]}

PROBES = {
    'callback_live_policy_sync': Probe(partial(execute, asynchronous=False), EXPECTED),
    'callback_live_policy_async': Probe(partial(execute, asynchronous=True), EXPECTED),
}
