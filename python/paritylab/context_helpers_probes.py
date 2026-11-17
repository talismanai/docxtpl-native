"""A standard Context.call method keeps its own bound receiver."""
from types import MethodType
from unittest.mock import patch
import jinja2
from jinja2.runtime import Context
from .protocol_base import Probe, generate, observed


def context_call_bound_receiver(engine):
    # Initialize the engine before changing the standard method. The binding
    # must honor later rebinding just as it honors ordinary user overrides.
    generate(engine, '{{ setup() }}', {'setup': lambda: 'ready'}, jinja2.Environment())
    environment = jinja2.Environment()
    donor = Context(jinja2.Environment(), {'scope': 'bound'}, 'donor', {}, globals={})
    trace = []
    @jinja2.pass_context
    def context(value):
        valid = value is donor and value.environment is donor.environment and value.name == 'donor'
        trace.append(valid)
        return value.get('scope', 'fresh')
    @jinja2.pass_eval_context
    def evaluation(value):
        valid = value is donor.eval_ctx
        trace.append(valid)
        return 'bound' if valid else 'fresh'
    @jinja2.pass_environment
    def environment_callback(value):
        valid = value is donor.environment
        trace.append(valid)
        return 'bound' if valid else 'fresh'
    with patch.object(Context, 'call', MethodType(Context.call, donor)):
        blob = generate(engine, '{{ context() }}|{{ evaluation() }}|{{ environment_callback() }}',
                        {'context': context, 'evaluation': evaluation, 'environment_callback': environment_callback}, environment)
    return observed(blob, trace)


PROBES = {
    'context_call_bound_receiver': Probe(context_call_bound_receiver,
        {'text': 'bound|bound|bound', 'trace': [True, True, True]})
}
