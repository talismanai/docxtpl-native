"""Identity across callbacks belongs to the render-context lifecycle."""
from functools import partial
import jinja2
from .protocol_base import Probe, generate, observed


def context_identity(engine, *, kind):
    contexts = []
    trace = []

    def callback(context):
        trace.append(context is contexts[0] if contexts else True)
        contexts.append(context)
        return 'X'

    decorator = jinja2.pass_context if kind == 'context' else jinja2.pass_eval_context
    blob = generate(engine, '{{ callback() }}{{ callback() }}',
                    {'callback': decorator(callback)}, jinja2.Environment())
    return observed(blob, trace)


PROBES = {
    'callback_context_identity': Probe(partial(context_identity, kind='context'),
                                      {'text': 'XX', 'trace': [True, True]}),
    'callback_eval_context_identity': Probe(partial(context_identity, kind='eval_context'),
                                           {'text': 'XX', 'trace': [True, True]}),
}
