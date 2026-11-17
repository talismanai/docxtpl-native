"""Iteration order and lazy loop metadata, with explicit reference oracles."""
from .protocol_base import Probe, Result, generate, observed

class IterationError(Exception):
    pass

def generator_length_metadata(engine):
    trace=[]
    def values():
        for value in range(3):trace.append('yield:'+str(value));yield value
    def tick():trace.append('tick');return ''
    return observed(generate(engine,'{% for value in values %}{{ value }}:{{ loop.length }}:{{ loop.revindex }};{{ tick() }}{% endfor %}',
        {'values':values(),'tick':tick}),trace)

def list_shrinks_during_loop(engine):
    values=[1,2,3];trace=[]
    def shrink(value):trace.append(value);del values[1:];return ''
    return observed(generate(engine,'{% for value in values %}{{ value }}{{ shrink(value) }}{% endfor %}',
        {'values':values,'shrink':shrink}),trace,final=values)

def list_grows_cached_length(engine):
    values=[1]
    def grow(value):
        if value==1:values.append(2)
        return ''
    return observed(generate(engine,'{% for value in values %}{{ loop.length }}/{{ loop.revindex0 }}={{ value }};{{ grow(value) }}{% endfor %}',
        {'values':values,'grow':grow}),final=values)

def loop_last_iterator_error(engine):
    trace=[];error=IterationError('peek failed')
    def values():
        trace.append('yield:1');yield 1
        trace.append('raise');raise error
    def after():trace.append('after');return ''
    try:generate(engine,'{% for value in values %}{{ value }}{{ loop.last }}{{ after() }}{% endfor %}',{'values':values(),'after':after})
    except IterationError as actual:return Result({'same_instance':actual is error,'message':str(actual),'trace':trace},{})
    raise AssertionError('loop.last swallowed iterator failure')

def loop_length_exception(engine):
    trace=[];error=IterationError('length failed')
    class Values:
        def __iter__(self):
            trace.append('iter')
            for value in range(3):trace.append('yield:'+str(value));yield value
        def __len__(self):trace.append('len');raise error
    def after():trace.append('after');return ''
    try:generate(engine,'{% for value in values %}{{ loop.length }}{{ after() }}{% endfor %}',{'values':Values(),'after':after})
    except IterationError as actual:return Result({'same_instance':actual is error,'message':str(actual),'trace':trace},{})
    raise AssertionError('loop.length swallowed __len__ failure')

def loop_repeated_last_end(engine):
    trace=[]
    class Values:
        count=0
        def __iter__(self):return self
        def __next__(self):
            trace.append('next:'+str(self.count));self.count+=1
            if self.count>1:raise StopIteration
            return 1
    return observed(generate(engine,'{% for value in values %}{{ loop.last }}|{{ loop.last }}{% endfor %}',{'values':Values()}),trace)

PROBES={
    'generator_length_metadata':Probe(generator_length_metadata,{'text':'0:3:3;1:3:2;2:3:1;','trace':['yield:0','yield:1','yield:2','tick','tick','tick']}),
    'list_shrinks_during_loop':Probe(list_shrinks_during_loop,{'text':'1','trace':[1],'final':[1]}),
    'list_grows_cached_length':Probe(list_grows_cached_length,{'text':'1/0=1;1/-1=2;','final':[1,2]}),
    'loop_last_iterator_error':Probe(loop_last_iterator_error,{'same_instance':True,'message':'peek failed','trace':['yield:1','raise']},True),
    'loop_length_exception':Probe(loop_length_exception,{'same_instance':True,'message':'length failed','trace':['iter','yield:0','len']},True),
    'loop_repeated_last_end':Probe(loop_repeated_last_end,{'text':'True|True','trace':['next:0','next:1','next:2','next:3']}),
}
