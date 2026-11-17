"""Observe sandbox policy descriptors before sync and async callbacks."""
import asyncio
import jinja2
from jinja2.sandbox import SandboxedEnvironment
from .protocol_base import Probe, generate, observed


def _call(engine, asynchronous):
    trace = []
    environment = SandboxedEnvironment(enable_async=asynchronous)
    class Callback:
        @property
        def unsafe_callable(self):
            trace.append('unsafe_callable')
            return False
        @property
        def alters_data(self):
            trace.append('alters_data')
            return False
        if asynchronous:
            @jinja2.pass_environment
            async def __call__(self, value, number):
                trace.append(['call', value is environment, number])
                await asyncio.sleep(0)
                trace.append('resumed')
                return str(number + 1)
        else:
            @jinja2.pass_environment
            def __call__(self, value, number):
                trace.append(['call', value is environment, number])
                return str(number + 1)
    blob = generate(engine, '{{ callback(value) }}', {'callback': Callback(), 'value': 41}, environment)
    return observed(blob, trace)


def sandbox_call_sync(engine):
    return _call(engine, False)


def sandbox_call_async(engine):
    return _call(engine, True)


PROBES = {
    'sandbox_call_sync': Probe(sandbox_call_sync, {'text': '42', 'trace': [
        'unsafe_callable', 'alters_data', ['call', True, 41]]}),
    'sandbox_call_async': Probe(sandbox_call_async, {'text': '42', 'trace': [
        'unsafe_callable', 'alters_data', ['call', True, 41], 'resumed']}),
}
