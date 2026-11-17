"""Original CPython operators, scalar identity and runtime library values."""
import jinja2
import operator
from unittest.mock import patch
from .protocol_base import Probe, Result, generate, observed

def arbitrary_precision_arithmetic(engine):
    n=2**220+7
    return observed(generate(engine,'{{ n+1 }}|{{ n*n }}|{{ -n }}|{{ n//3 }}|{{ n%3 }}',{'n':n}))

def reflected_operator(engine):
    trace=[]
    class Right:
        def __radd__(self,left):trace.append(['radd',left]);return 'reflected'
    return observed(generate(engine,'{{ 3+right }}',{'right':Right()}),trace)

def operator_module_mutation(engine):
    def wrong(*args):raise AssertionError('operator module unexpectedly used')
    with patch.object(operator,'neg',wrong), patch.object(operator,'pos',wrong), patch.object(operator,'contains',wrong):
        return observed(generate(engine,'{{ -value }}|{{ +value }}|{{ key in values }}|{{ key not in values }}',
                                 {'value':4,'key':2,'values':[1,2]}))

def concat_evaluation_order(engine):
    trace=[]
    class Value:
        def __init__(self, name):self.name=name
        def __str__(self):trace.append('str:'+self.name);return self.name
    def get(name):trace.append('get:'+name);return Value(name)
    return observed(generate(engine,'{{ get("a") ~ get("b") ~ get("c") }}',{'get':get}),trace)

def operator_error_identity(engine):
    trace=[];error=ValueError('arithmetic failure')
    class Broken:
        def __mul__(self,right):trace.append(['mul',right]);raise error
    def after():trace.append('after');return ''
    try:generate(engine,'{{ value*2 }}{{ after() }}',{'value':Broken(),'after':after})
    except ValueError as actual:
        return Result({'type':type(actual).__module__+'.'+type(actual).__name__,
                       'same_instance':actual is error,'message':str(actual),'trace':trace},{})
    raise AssertionError('operator exception was suppressed')

def rich_comparison_short_circuit(engine):
    trace=[]
    class Truth:
        def __bool__(self):trace.append('bool');return False
        def __str__(self):return 'comparison-result'
    result=Truth()
    class Left:
        def __lt__(self,right):trace.append(['lt',right]);return result
    def after():trace.append('after');return 3
    def capture(actual):trace.append(actual is result);return actual
    return observed(generate(engine,'{{ capture(value < 2 < after()) }}',
                             {'value':Left(),'after':after,'capture':capture}),trace)

def original_scalar_identity(engine):
    values=[int('123456789123456789'),float('12.3456789'),''.join(['original',' string']),bytes([21,42])]
    trace=[]
    def check(value,index):trace.append(value is values[index]);return ''
    return observed(generate(engine,'{% for value in values %}{{ check(value,loop.index0) }}{% endfor %}'
                             '{{ values[0].bit_length() }}|{{ values[2].upper() }}',
                             {'values':values,'check':check}),trace)

def slice_protocol(engine):
    trace=[]
    class Source:
        def __getitem__(self,key):
            trace.append([type(key).__name__,key.start,key.stop,key.step]);return 'slice'
    return observed(generate(engine,'{{ value[1:7:2] }}|{{ text[::-1] }}',
                             {'value':Source(),'text':'abc'}),trace)

def runtime_builtins(engine):
    trace=[];environment=jinja2.Environment()
    def check(value):trace.append(type(value).__module__+'.'+type(value).__name__);return ''
    environment.filters['check']=check
    source="{% set ns=namespace(count=0) %}{% for i in range(3) %}{% set ns.count=ns.count+i %}{% endfor %}" \
           "{{ ns|check }}{{ ns.count }}|{% set c=cycler('a','b') %}{{ c|check }}{{ c.next() }}{{ c.next() }}|" \
           "{{ values|sort|check }}{{ range(2)|check }}{{ mapping|items|check }}"
    return observed(generate(engine,source,{'values':[3,1,2],'mapping':{'x':1}},environment),trace)

def compile_time_filter_order(engine):
    trace=[];environment=jinja2.Environment()
    def mark(value):trace.append(value);return value
    environment.filters['mark']=mark
    def runtime():trace.append('runtime');return ''
    return observed(generate(engine,"{{ runtime() }}{{ 'constant'|mark }}",{'runtime':runtime},environment),trace)

def literal_containers(engine):
    trace=[];previous=[]
    def check(value):trace.append(type(value).__name__);previous.append(value);return ''
    def same(value):trace.append(value is previous[0]);return ''
    source="{% set value=[] %}{{ check(value) }}{{ value.append('x') }}{{ same(value) }}{{ value }}|" \
           "{{ check((1,2)) }}{{ check({'a':[3]}) }}{{ check(()) }}"
    return observed(generate(engine,source,{'same':same,'check':check}),trace)

def unary_plus_protocol(engine):
    trace=[]
    class Number:
        def __pos__(self):trace.append('pos');return 7
    return observed(generate(engine,'{{ +value }}',{'value':Number()}),trace)

def constant_finalizer_order(engine):
    trace=[]
    def finalize(value):trace.append('finalize:'+value);return value
    environment=jinja2.Environment(finalize=finalize)
    def mark(value):trace.append('filter:'+value);return value
    def runtime():trace.append('runtime');return 'R'
    environment.filters['mark']=mark
    return observed(generate(engine,"{{ runtime() }}{{ 'Café 🐍'|mark }}",{'runtime':runtime},environment),trace)

def constant_autoescape_scope(engine):
    trace=[];environment=jinja2.Environment()
    @jinja2.pass_eval_context
    def mark(context,value):trace.append([value,context.autoescape,type(context) is jinja2.nodes.EvalContext]);return value
    def runtime():trace.append('runtime');return 'R'
    environment.filters['mark']=mark
    source="{% autoescape true %}{{ 'S'|mark }}{% endautoescape %}{{ runtime() }}{% autoescape enabled %}{{ 'D'|mark }}{% endautoescape %}"
    return observed(generate(engine,source,{'runtime':runtime,'enabled':True},environment),trace)

def constant_abort_order(engine):
    trace=[];error=KeyboardInterrupt('compile abort');environment=jinja2.Environment()
    def abort(value):trace.append('abort');raise error
    def runtime():trace.append('runtime');return ''
    environment.filters['abort']=abort
    try:generate(engine,"{{ runtime() }}{{ 'x'|abort }}",{'runtime':runtime},environment)
    except KeyboardInterrupt as actual:
        return Result({'same_instance':actual is error,'trace':trace,'message':str(actual)},{})
    raise AssertionError('compiler abort was ignored')

def constant_argument_order(engine):
    trace=[];environment=jinja2.Environment()
    def mark(value):trace.append('constant');return value
    def runtime():trace.append('runtime');return ''
    def consume(value):trace.append('consume:'+value);return value
    environment.filters['mark']=mark
    return observed(generate(engine,"{{ runtime() }}{{ consume('x'|mark) }}",{'runtime':runtime,'consume':consume},environment),trace)

def constant_argument_mutable(engine):
    trace=[]; environment=jinja2.Environment()
    def mark(value): trace.append('constant'); return [value]
    def runtime(): trace.append('runtime'); return ''
    def consume(value): trace.append([type(value).__name__, value[:]]); value.append('changed'); return 'ok'
    environment.filters['mark']=mark
    return observed(generate(engine,"{{ runtime() }}{{ consume('x'|mark) }}",{'runtime':runtime,'consume':consume},environment),trace)

def constant_argument_unsafe(engine):
    trace=[]; environment=jinja2.Environment(); value=object()
    def mark(_): trace.append('mark'); return value
    def runtime(): trace.append('runtime'); return ''
    def consume(actual): trace.append(actual is value); return 'ok'
    environment.filters['mark']=mark
    return observed(generate(engine,"{{ runtime() }}{{ consume('x'|mark) }}",{'runtime':runtime,'consume':consume},environment),trace)

def constant_argument_failure(engine):
    trace=[]; error=ValueError('filter failed'); environment=jinja2.Environment()
    def mark(_): trace.append('mark'); raise error
    def runtime(): trace.append('runtime'); return ''
    environment.filters['mark']=mark
    try: generate(engine,"{{ runtime() }}{{ consume('x'|mark) }}",{'runtime':runtime,'consume':lambda x:x},environment)
    except ValueError as actual: return Result({'same_instance':actual is error,'trace':trace},{})
    raise AssertionError('filter error was ignored')

def constant_argument_unoptimized(engine):
    trace=[]; environment=jinja2.Environment(optimized=False)
    def mark(value): trace.append('mark'); return value
    def runtime(): trace.append('runtime'); return ''
    def consume(value): trace.append('consume'); return value
    environment.filters['mark']=mark
    return observed(generate(engine,"{{ runtime() }}{{ consume('x'|mark) }}",{'runtime':runtime,'consume':consume},environment),trace)

def constant_argument_context_lookup(engine):
    trace=[];environment=jinja2.Environment()
    class ContextFilter:
        jinja_pass_arg=jinja2.utils._PassArg.context
        def __getattribute__(self,name):
            if name=='jinja_pass_arg':trace.append('marker')
            return object.__getattribute__(self,name)
        def __call__(self,context,value):trace.append('filter');return value
    def runtime():trace.append('runtime');return ''
    environment.filters['mark']=ContextFilter()
    return observed(generate(engine,"{{ runtime() }}{{ consume('x'|mark) }}",{'runtime':runtime,'consume':lambda x:x},environment),trace)

def constant_multiline_order(engine):
    trace=[];environment=jinja2.Environment()
    def mark(value):trace.append('constant');return value
    def runtime():trace.append('runtime');return ''
    environment.filters['mark']=mark
    return observed(generate(engine,"{{ runtime() }}{{\n 'X'|mark\n }}",{'runtime':runtime},environment),trace)

def original_test_result(engine):
    trace=[];environment=jinja2.Environment()
    class Result:
        def __bool__(self):trace.append('bool');return False
        def __str__(self):return 'test-object'
    result=Result()
    def custom(value):trace.append('test');return result
    def capture(value):trace.append(value is result);return value
    environment.tests['custom']=custom
    source="{% set result=value is custom %}{{ capture(result) }}{% if result %}yes{% endif %}"
    return observed(generate(engine,source,{'value':7,'capture':capture},environment),trace)

n=2**220+7
PROBES={
    'arbitrary_precision_arithmetic':Probe(arbitrary_precision_arithmetic,{'text':f'{n+1}|{n*n}|{-n}|{n//3}|{n%3}'}),
    'reflected_operator':Probe(reflected_operator,{'text':'reflected','trace':[['radd',3]]}),
    'operator_module_mutation':Probe(operator_module_mutation,{'text':'-4|4|True|False'}),
    'concat_evaluation_order':Probe(concat_evaluation_order,{'text':'abc','trace':['get:a','get:b','get:c','str:a','str:b','str:c']}),
    'operator_error_identity':Probe(operator_error_identity,{'type':'builtins.ValueError','same_instance':True,'message':'arithmetic failure','trace':[['mul',2]]},True),
    'rich_comparison_short_circuit':Probe(rich_comparison_short_circuit,{'text':'comparison-result','trace':[['lt',2],'bool',True]}),
    'original_scalar_identity':Probe(original_scalar_identity,{'text':'57|ORIGINAL STRING','trace':[True]*4}),
    'slice_protocol':Probe(slice_protocol,{'text':'slice|cba','trace':[['slice',1,7,2]]}),
    'runtime_builtins':Probe(runtime_builtins,{'text':'3|ab|','trace':['jinja2.utils.Namespace','jinja2.utils.Cycler','builtins.list','builtins.range','builtins.generator']}),
    'compile_time_filter_order':Probe(compile_time_filter_order,{'text':'constant','trace':['constant','runtime']}),
    'literal_containers':Probe(literal_containers,{'text':"None['x']|",'trace':['list',True,'tuple','dict','tuple']}),
    'unary_plus_protocol':Probe(unary_plus_protocol,{'text':'7','trace':['pos']}),
    'constant_finalizer_order':Probe(constant_finalizer_order,{'text':'RCafé 🐍','trace':['filter:Café 🐍','finalize:Café 🐍','runtime','finalize:R']}),
    'constant_autoescape_scope':Probe(constant_autoescape_scope,{'text':'SRD','trace':[['S',True,True],'runtime',['D',True,True]]}),
    'constant_abort_order':Probe(constant_abort_order,{'same_instance':True,'trace':['abort'],'message':'compile abort'},True),
    'constant_argument_order':Probe(constant_argument_order,{'text':'x','trace':['constant','runtime','consume:x']}),
    'constant_argument_mutable':Probe(constant_argument_mutable,{'text':'ok','trace':['constant','runtime',['list',['x']]]}),
    'constant_argument_unsafe':Probe(constant_argument_unsafe,{'text':'ok','trace':['mark','mark','runtime','mark',True]}),
    'constant_argument_failure':Probe(constant_argument_failure,{'same_instance':True,'trace':['mark','mark','runtime','mark']},True),
    'constant_argument_unoptimized':Probe(constant_argument_unoptimized,{'text':'x','trace':['runtime','mark','consume']}),
    'constant_argument_context_lookup':Probe(constant_argument_context_lookup,{'text':'x','trace':['marker']*6+['runtime','filter']}),
    'constant_multiline_order':Probe(constant_multiline_order,{'text':'X','trace':['constant','runtime']}),
    'original_test_result':Probe(original_test_result,{'text':'test-object','trace':['test',True,'bool']}),
}
