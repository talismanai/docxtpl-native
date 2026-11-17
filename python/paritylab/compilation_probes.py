"""Compilation must precede conversion of a context mapping, for every part."""
from collections.abc import Mapping
import io
import jinja2
from docx import Document
from .protocol_base import Probe, Result, generate, observed

class Context(Mapping):
    def __init__(self,trace):self.trace=trace;self.data={'value':'before'}
    def __iter__(self):self.trace.append('snapshot');return iter(self.data)
    def __len__(self):return len(self.data)
    def __getitem__(self,key):return self.data[key]

def compilation_before_snapshot(engine):
    trace=[];context=Context(trace);environment=jinja2.Environment()
    def change(value):trace.append('compile');context.data['value']='after';return value
    environment.filters['change']=change
    return observed(generate(engine,"{{ value }}{{ ''|change }}",context,environment),trace)

def compilation_abort_before_snapshot(engine):
    trace=[];context=Context(trace);environment=jinja2.Environment();error=KeyboardInterrupt('compiler stopped')
    def abort(value):trace.append('compile');raise error
    environment.filters['abort']=abort
    try:generate(engine,"{{ value }}{{ ''|abort }}",context,environment)
    except KeyboardInterrupt as actual:return Result({'trace':trace,'same_instance':actual is error},{})
    raise AssertionError('compiler exception was suppressed')

def property_compilation_before_snapshot(engine):
    trace=[];context=Context(trace);environment=jinja2.Environment()
    def change(value):trace.append('compile');context.data['value']='after';return value
    environment.filters['change']=change
    source=Document();source.add_paragraph('{{ value }}')
    source.core_properties.author="{{ 'C'|change }}";source.core_properties.title='{{ value }}'
    buffer=io.BytesIO();source.save(buffer)
    output=generate(engine,'',context,environment,source=buffer.getvalue())
    document=Document(io.BytesIO(output))
    return observed(output,trace,author=document.core_properties.author,title=document.core_properties.title)


def simple_name_source(engine):
    return observed(generate(engine,'Café {{ value }}|{{\nvalue\t}}|{{ number }}|{{ flag }}|{{ empty }}',
                             {'value':'X','number':10**100,'flag':False,'empty':None}))

PROBES={
    'compilation_before_snapshot':Probe(compilation_before_snapshot,{'text':'after','trace':['compile']+['snapshot']*7}),
    'compilation_abort_before_snapshot':Probe(compilation_abort_before_snapshot,{'trace':['compile'],'same_instance':True},True),
    'property_compilation_before_snapshot':Probe(property_compilation_before_snapshot,{'text':'before','trace':['snapshot','compile']+['snapshot']*6,'author':'C','title':'after'}),
    'simple_name_source':Probe(simple_name_source,{'text':'Café X|X|'+str(10**100)+'|False|None'}),
}
