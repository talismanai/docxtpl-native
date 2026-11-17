"""Finalizer call shape is captured by compilation; the callable stays live."""
import jinja2
from .protocol_base import Probe, Result, generate, observed

def finalizer_enabled_during_render(engine):
    trace=[];environment=jinja2.Environment()
    def finalize(value):trace.append('unexpected finalize');return 'CHANGED'
    def enable():trace.append('enable');environment.finalize=finalize;return 'E'
    def disable():trace.append('disable');environment.finalize=None;return 'D'
    return observed(generate(engine,'{{ enable() }}|{{ value }}|{{ disable() }}',
                             {'enable':enable,'disable':disable,'value':'X'},environment),trace)

def finalizer_removed_during_render(engine):
    trace=[];environment=jinja2.Environment(finalize=lambda value:value)
    def disable():trace.append('disable');environment.finalize=None;return 'D'
    def after():trace.append('after');return ''
    try:generate(engine,'{{ disable() }}{{ after() }}',{'disable':disable,'after':after},environment)
    except TypeError as error:return Result({'type':type(error).__module__+'.'+type(error).__name__,'message':str(error),'trace':trace},{})
    raise AssertionError('compiled finalizer call was removed')

def falsey_finalizer(engine):
    trace=[]
    class Finalizer:
        def __bool__(self):trace.append('bool');return False
        def __call__(self,value):trace.append('unexpected call');return 'CHANGED'
    return observed(generate(engine,'{{ value }}',{'value':'X'},jinja2.Environment(finalize=Finalizer())),trace)

def scalar_escape_identity(engine):
    value="A<&>'\"🐍";trace=[]
    def marker(actual):trace.append(actual is value);return actual
    return observed(generate(engine,'{{ value }}|{{ marker(value) }}',{'value':value,'marker':marker},
                             jinja2.Environment(autoescape=True)),trace)

PROBES={
    'finalizer_enabled_during_render':Probe(finalizer_enabled_during_render,{'text':'E|X|D','trace':['enable','disable']}),
    'finalizer_removed_during_render':Probe(finalizer_removed_during_render,{'type':'builtins.TypeError','message':"'NoneType' object is not callable",'trace':['disable','after']},True),
    'falsey_finalizer':Probe(falsey_finalizer,{'text':'X','trace':['bool']*3}),
    'scalar_escape_identity':Probe(scalar_escape_identity,{'text':"A<&>'\"🐍|A<&>'\"🐍",'trace':[True]}),
}
