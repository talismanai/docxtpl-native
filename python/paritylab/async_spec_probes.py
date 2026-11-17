"""Observable async runtime partitions against the pinned Jinja reference."""
import asyncio
import io
import warnings
from typing import Any

import jinja2
from docx import Document

from .protocol_base import Probe, Result, generate, observed, template


def async_order(engine):
    trace=[]; loops=[]; tasks=[]
    async def call(value):
        trace.append('start:'+value)
        loops.append(asyncio.get_running_loop());tasks.append(asyncio.current_task())
        await asyncio.sleep(0)
        trace.append('finish:'+value)
        return value
    blob=generate(engine,"{{ call('A') }}{{ call('B') }}",{'call':call},jinja2.Environment(enable_async=True))
    return observed(blob,trace,same_loop=loops[0] is loops[1],same_task=tasks[0] is tasks[1])


def async_exception(engine):
    trace=[];failure=ValueError('async failure')
    async def call():
        trace.append('start');await asyncio.sleep(0);trace.append('raise');raise failure
    def later():trace.append('later');return ''
    try:generate(engine,'{{ call() }}{{ later() }}',{'call':call,'later':later},jinja2.Environment(enable_async=True))
    except ValueError as error:return Result({'same_instance':error is failure,'message':str(error),'trace':trace},{})
    raise AssertionError('expected asynchronous callback failure')


def sync_return_awaitable(engine):
    trace=[]
    async def inner():trace.append('awaited');await asyncio.sleep(0);return 'X'
    def call():trace.append('called');return inner()
    return observed(generate(engine,'{{ call() }}',{'call':call},jinja2.Environment(enable_async=True)),trace)


def async_filter(engine):
    trace=[];environment=jinja2.Environment(enable_async=True)
    async def decorate(value):trace.append('filter:'+value);await asyncio.sleep(0);return value.upper()
    environment.filters['decorate']=decorate
    return observed(generate(engine,'{{ value|decorate }}',{'value':'x'},environment),trace)


def async_iterable(engine):
    trace=[]
    async def values():
        trace.append('yield:A');yield 'A';await asyncio.sleep(0);trace.append('yield:B');yield 'B'
    return observed(generate(engine,'{% for item in values %}{{ item }}{% endfor %}',{'values':values()},jinja2.Environment(enable_async=True)),trace)


def running_event_loop(engine):
    trace=[]
    async def run():
        try:generate(engine,'{{ callback() }}',{'callback':lambda:trace.append('callback')},jinja2.Environment(enable_async=True))
        except RuntimeError as error:return Result({'message':str(error),'trace':trace},{})
        raise AssertionError('expected active event loop failure')
    with warnings.catch_warnings():
        warnings.simplefilter('ignore',RuntimeWarning)
        return asyncio.run(run())

def async_file_subdoc_parts(engine: Any) -> Result:
    # The unused native child is the same routing fixture in both roles.
    import docxtpl_native

    child = object.__new__(docxtpl_native.Subdoc)
    child.tpl = None
    child.source = io.BytesIO(template('child'))
    trace: list[str] = []
    tasks: dict[str, asyncio.Task[Any]] = {}

    async def callback(label: str) -> str:
        task = asyncio.current_task()
        assert task is not None
        tasks[label] = task
        trace.append('start:' + label)
        await asyncio.sleep(0)
        trace.append('finish:' + label)
        return label

    source = template("{{ callback('body') }}", header="{{ callback('header') }}")
    output = generate(engine, '', {'child': child, 'callback': callback},
                      jinja2.Environment(enable_async=True), source)
    document = Document(io.BytesIO(output))
    return Result({'text': document.paragraphs[0].text,
                   'header': document.sections[0].header.paragraphs[0].text,
                   'trace': trace, 'separate_tasks': tasks['body'] is not tasks['header']},
                  {'document.docx': output})

PROBES={
 'async_callback_order':Probe(async_order,{'text':'AB','trace':['start:A','finish:A','start:B','finish:B'],'same_loop':True,'same_task':True}),
 'async_callback_exception':Probe(async_exception,{'same_instance':True,'message':'async failure','trace':['start','raise']},True),
 'async_sync_returns_awaitable':Probe(sync_return_awaitable,{'text':'X','trace':['called','awaited']}),
 'async_filter_callback':Probe(async_filter,{'text':'X','trace':['filter:x']}),
 'async_iterable_loop':Probe(async_iterable,{'text':'AB','trace':['yield:A','yield:B']}),
 'async_running_event_loop':Probe(running_event_loop,{'message':'asyncio.run() cannot be called from a running event loop','trace':[]},True),
 'async_file_subdoc_parts':Probe(async_file_subdoc_parts,{
     'text':'body','header':'header','trace':['start:body','finish:body','start:header','finish:header'],
     'separate_tasks':True}),
}
