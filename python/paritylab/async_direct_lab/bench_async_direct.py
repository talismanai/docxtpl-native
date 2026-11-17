"""Paired microbenchmark of callback dispatch, excluding DOCX generation."""
import argparse
import asyncio
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import platform
import statistics
import time


async def reference(callbacks):
    values=[]
    for callback in callbacks:
        value=callback()
        values.append(await value if inspect.isawaitable(value) else value)
    return values


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--extension',type=Path,required=True)
    parser.add_argument('--count',type=int,default=200)
    parser.add_argument('--pairs',type=int,default=9)
    args=parser.parse_args()
    if args.count<1 or args.pairs<1:parser.error('count and pairs must be positive')
    extension=args.extension.resolve()
    spec=importlib.util.spec_from_file_location('_async_direct',str(extension))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    async def async_callback():
        await asyncio.sleep(0)
        return 42
    def sync_callback():return 42
    async def run():
        outcomes={}
        for label,callback in [('synchronous',sync_callback),('async_sleep_zero',async_callback)]:
            callbacks=[callback]*args.count
            async def measure(mode):
                start=time.perf_counter_ns()
                result=await (reference(callbacks) if mode=='reference' else
                              module.run_direct(callbacks) if mode=='direct' else module.run_from_worker(callbacks))
                duration=time.perf_counter_ns()-start
                if result!=[42]*args.count:raise AssertionError('incorrect result')
                return duration
            for mode in ('reference','direct','worker'):await measure(mode)
            samples=[]
            orders=(('reference','direct','worker'),('worker','reference','direct'),('direct','worker','reference'))
            for index in range(args.pairs):
                pair={}
                for mode in orders[index%len(orders)]:pair[mode]=await measure(mode)
                samples.append(pair)
            outcomes[label]={'samples':samples,
                'median_reference_over_direct':statistics.median(pair['reference']/pair['direct'] for pair in samples),
                'median_reference_over_worker':statistics.median(pair['reference']/pair['worker'] for pair in samples)}
        return outcomes
    result=asyncio.run(run())
    print(json.dumps({'python':platform.python_version(),'callback_count':args.count,'pairs':args.pairs,
        'extension_sha256':hashlib.sha256(extension.read_bytes()).hexdigest(),'workloads':result,
        'scope':'Sequential callback dispatch in a running asyncio loop; direct and worker prototypes vs small Python oracle, not DOCX or Jinja generation.'},indent=2))


if __name__=='__main__':main()
