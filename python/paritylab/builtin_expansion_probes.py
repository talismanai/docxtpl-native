"""Curated builtin behavior partitions. Literal expected outputs are independent."""
import jinja2
from .protocol_base import Probe, generate, observed

# Every case uses a runtime input, including cases with otherwise constant arguments.
FILTERS={
 'abs':('value|abs',-3,'3'), 'capitalize':('value|capitalize','hELLO','Hello'),
 'center':('value|center(5)','x','  x  '), 'count':('value|count',[1,2],'2'),
 'd':("value|d('fallback',true)",'','fallback'), 'default':("value|default('fallback',true)",False,'fallback'),
 'dictsort':("value|dictsort|map(attribute=0)|join(',')",{'b':2,'a':1},'a,b'),
 'filesizeformat':('value|filesizeformat',1000,'1.0 kB'), 'float':('value|float','1.5','1.5'),
 'format':("value|format('X')",'%s','X'), 'indent':('value|indent(2,true)','X','  X'),
 'int':('value|int','42','42'), 'list':("value|list|join(',')",'AB','A,B'),
 'items':("value|items|map(attribute=0)|join(',')",{'a':1,'b':2},'a,b'),
 'map':("value|map('upper')|join(',')",['a','b'],'A,B'),
 'min':('value|min',[3,1,2],'1'), 'max':('value|max',[3,1,2],'3'),
 'pprint':('value|pprint',[1,2],'[1, 2]'), 'random':('value|random',['only'],'only'),
 'reject':("value|reject('odd')|join(',')",[1,2,3,4],'2,4'),
 'rejectattr':("value|rejectattr('enabled')|map(attribute='name')|join(',')",[{'name':'A','enabled':True},{'name':'B','enabled':False}],'B'),
 'reverse':('value|reverse','abc','cba'), 'round':('value|round(1)',1.25,'1.2'),
 'select':("value|select('odd')|join(',')",[1,2,3,4],'1,3'),
 'selectattr':("value|selectattr('enabled')|map(attribute='name')|join(',')",[{'name':'A','enabled':True},{'name':'B','enabled':False}],'A'),
 'sort':("value|sort|join(',')",[3,1,2],'1,2,3'), 'string':('value|string',42,'42'),
 'striptags':('value|striptags','<b>hello</b>','hello'), 'title':('value|title','hello world','Hello World'),
 'trim':('value|trim',' X ','X'), 'truncate':('value|truncate(5,true,\'...\',0)','abcdefghij','ab...'),
 'unique':("value|unique|join(',')",['A','A','B'],'A,B'),
 'urlencode':('value|urlencode','a b','a%20b'), 'wordcount':('value|wordcount','one two','2'),
 'wordwrap':("value|wordwrap(3,wrapstring='|')",'one two','one|two'),
 'tojson':('value|tojson',[1,2],'[1, 2]'),
}
# True and false input partitions for each named predicate.
TESTS={
 'odd':(3,2,None),'even':(2,3,None),'divisibleby':(6,7,3),
 'defined':('X',None,None), 'none':(None,'X',None), 'boolean':(True,1,None),
 'false':(False,0,None),'true':(True,1,None),'integer':(1,True,None),'float':(1.5,1,None),
 'lower':('abc','ABC',None),'upper':('ABC','abc',None),'string':('X',1,None),
 'mapping':({},[],None),'number':(1,'1',None),'sequence':([],1,None),
 'iterable':([],1,None),'callable':(str,1,None),
 'in':('a','z',['a','b']),
}

def output(source,context,env=None):
    def execute(engine):return observed(generate(engine,source,context,env))
    return execute

FILTERS.update({
 'attr':("value|attr('name')",__import__('types').SimpleNamespace(name='X'),'X'),
 'batch':("value|batch(2)|map('join')|join('|')",[1,2,3,4,5],'12|34|5'),
 'slice':("value|slice(2)|map('join')|join('|')",[1,2,3,4,5],'123|45'),
 'groupby':("value|groupby('category')|map(attribute='grouper')|join(',')",[{'category':'b'},{'category':'a'}],'a,b'),
 'e':('value|e','<','<'), 'escape':('value|escape','<','<'),
 'forceescape':('value|safe|forceescape','<','<'),
 'safe':('value|safe is escaped','X','True'),
 'urlize':('value|urlize|striptags','example.com','example.com'),
 'xmlattr':('value|xmlattr',{'a':'1'},' a="1"'),
})
TESTS.update({
 'eq':(2,3,2),'equalto':(2,3,2),'ne':(3,2,2),
 'gt':(3,1,2),'greaterthan':(3,1,2),'ge':(2,1,2),
 'lt':(1,3,2),'lessthan':(1,3,2),'le':(2,3,2),
})
PROBES={}
for name,(expression,value,expected) in FILTERS.items():
    PROBES['spec_filter_'+name]=Probe(output('{{ '+expression+' }}',{'value':value}),{'text':expected})
for name,(yes,no,arg) in TESTS.items():
    if name=='defined':continue # Undefined requires an actually missing name, not None.
    expression='{{ value is '+name+('(arg)' if arg is not None else '')+' }}'
    for label,value,expected in [('true',yes,'True'),('false',no,'False')]:
        PROBES['spec_test_'+name+'_'+label]=Probe(output(expression,{'value':value,'arg':arg}),{'text':expected})
for name,source,expected in [
 ('defined', '{{ missing is defined }}|{{ value is defined }}','False|True'),
 ('undefined','{{ missing is undefined }}|{{ value is undefined }}','True|False'),
 ('filter',"{{ 'upper' is filter }}|{{ 'nonexistent_builtin' is filter }}",'True|False'),
 ('test',"{{ 'odd' is test }}|{{ 'nonexistent_builtin' is test }}",'True|False'),
 ('sameas','{{ value is sameas(value) }}|{{ value is sameas(other) }}','True|False'),
 ('escaped',"{{ value is escaped }}|{{ value|safe is escaped }}",'False|True'),
]:
    PROBES['spec_test_'+name]=Probe(output(source,{'value':'X','other':object()}),{'text':expected})
for name,source,expected in [
 ('range',"{{ range(1,5,2)|join(',') }}",'1,3'),
 ('dict',"{% set d=dict(a=1,b=2) %}{{ d.a }}|{{ d.b }}",'1|2'),
 ('lipsum','{{ lipsum(0,false) }}',''),
 ('cycler',"{% set c=cycler('A','B') %}{{ c.next() }}{{ c.next() }}{{ c.current }}",'ABA'),
 ('joiner',"{% set j=joiner('|') %}{{ j() }}A{{ j() }}B",'A|B'),
 ('namespace','{% set n=namespace(x=0) %}{% set n.x=2 %}{{ n.x }}','2'),
]:
    PROBES['spec_global_'+name]=Probe(output(source,{}),{'text':expected})

# Symbolic aliases are registry keys, not legal names after Jinja's `is` token.
# Exercise the public Environment.call_test binding through a native callback.
def registry_alias(name,yes,no,arg):
    def execute(engine):
        env=jinja2.Environment()
        def check(value):return env.call_test(name,value,[arg])
        return observed(generate(engine,'{{ check(yes) }}|{{ check(no) }}',{'check':check,'yes':yes,'no':no},env))
    return execute
for alias,yes,no,arg in [('==',2,3,2),('!=',3,2,2),('>',3,1,2),('>=',2,1,2),('<',1,3,2),('<=',2,3,2)]:
    PROBES['spec_test_alias_'+str(ord(alias[0]))+'_'+str(len(alias))]=Probe(registry_alias(alias,yes,no,arg),{'text':'True|False'})
