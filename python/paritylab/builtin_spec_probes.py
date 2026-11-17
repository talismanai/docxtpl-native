"""Independent expected outputs for named Jinja builtin input partitions."""
from .protocol_base import Probe, Result, generate, observed

# Expected values are curated literals, not computed with either engine.
PARTITIONS={
 'upper': [('normal','{{ value|upper }}','café','CAFÉ'),('empty','{{ value|upper }}','','')],
 'lower': [('normal','{{ value|lower }}','CAFÉ','café'),('empty','{{ value|lower }}','','')],
 'replace': [('normal',"{{ value|replace('a','x',1) }}",'banana','bxnana'),('empty',"{{ value|replace('a','x') }}",'','')],
 'join': [('normal',"{{ value|join(',') }}",['A','B'],'A,B'),('empty',"{{ value|join(',') }}",[],'')],
 'sum': [('normal','{{ value|sum }}',[1,2,3],'6'),('empty','{{ value|sum }}',[],'0')],
 'length': [('normal','{{ value|length }}',['A','B'],'2'),('empty','{{ value|length }}',[],'0')],
 'first': [('normal','{{ value|first }}',['A','B'],'A'),('empty','{{ value|first }}',[],'')],
 'last': [('normal','{{ value|last }}',['A','B'],'B'),('empty','{{ value|last }}',[],'')],
}

def output_probe(source,value):
    def execute(engine):return observed(generate(engine,source,{'value':value}))
    return execute

def length_invalid(engine):
    try:generate(engine,'{{ value|length }}',{'value':3})
    except TypeError as error:return Result({'type':'builtins.TypeError','message':str(error)},{})
    raise AssertionError('length on integer should raise TypeError')

PROBES={}
for name,partitions in PARTITIONS.items():
    for partition,source,value,expected in partitions:
        PROBES['builtin_'+name+'_'+partition]=Probe(output_probe(source,value),{'text':expected})
PROBES['builtin_length_invalid']=Probe(length_invalid,{'type':'builtins.TypeError','message':"object of type 'int' has no len()"},True)
