"""Macro binding, original container types and invocation-time defaults."""
import jinja2
from .protocol_base import Probe, generate, observed

def macro_variadic_types(engine):
    trace=[];environment=jinja2.Environment()
    def tuple_check(value):trace.append(type(value) is tuple);return 'tuple:'+','.join(map(str,value))
    def mapping_check(value):trace.append(type(value) is dict);return 'dict'
    environment.filters.update(tuple_check=tuple_check,mapping_check=mapping_check)
    source="{% macro m(a='A') %}{{ varargs|tuple_check }}|{{ kwargs|mapping_check }}|{{ kwargs.pop('x') }}|{{ kwargs.get('x','gone') }}{% endmacro %}{{ m('A',2,3,x=7) }}"
    return observed(generate(engine,source,{},environment),trace)

def macro_defaults_dependencies(engine):
    return observed(generate(engine,"{% macro m(a,b=a,c=b) %}{{ a }}|{{ b }}|{{ c }}{% endmacro %}{{ m('X') }}",{'a':'outer','b':'outer'}))

def macro_positional_and_captured_keyword(engine):
    return observed(generate(engine,"{% macro m(a) %}{{ a }}|{{ kwargs.a }}{% endmacro %}{{ m(1,a=2) }}",{}))

def macro_metadata(engine):
    previous=[];trace=[];environment=jinja2.Environment()
    def check(value):
        trace.append(type(value) is tuple)
        if previous:trace.append(value is previous[0])
        previous.append(value);return ''
    environment.filters['check']=check
    source="{% macro m(a,b=2) %}{{ varargs }}{{ kwargs }}{% endmacro %}{{ m.arguments|check }}{{ m.arguments|check }}{{ m.arguments }}|{{ m.catch_varargs }}|{{ m.catch_kwargs }}|{{ m.caller }}"
    return observed(generate(engine,source,{},environment),trace)

def huge_integer_identity(engine):
    value=2**220+7;trace=[]
    def check(actual):trace.append(actual is value);return actual
    return observed(generate(engine,'{{ check(value) }}',{'value':value,'check':check}),trace)

PROBES={
    'macro_variadic_types':Probe(macro_variadic_types,{'text':'tuple:2,3|dict|7|gone','trace':[True,True]}),
    'macro_defaults_dependencies':Probe(macro_defaults_dependencies,{'text':'X|X|X'}),
    'macro_positional_and_captured_keyword':Probe(macro_positional_and_captured_keyword,{'text':'1|2'}),
    'macro_metadata':Probe(macro_metadata,{'text':"('a', 'b')|True|True|False",'trace':[True,True,True]}),
    'huge_integer_identity':Probe(huge_integer_identity,{'text':str(2**220+7),'trace':[True]}),
}
