"""Interleaved baseline/fast-path callback microbenchmark; not a renderer."""
import argparse
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import statistics
import time

from jinja2.async_utils import auto_await


def load(path):
    spec = importlib.util.spec_from_file_location('_async_direct', str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def reference(callbacks):
    return [await auto_await(callback()) for callback in callbacks]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--count', type=int, default=200)
    parser.add_argument('--pairs', type=int, default=21)
    args = parser.parse_args()
    if args.count < 1 or args.pairs < 1:
        parser.error('count and pairs must be positive')
    paths = {name: getattr(args, name).resolve() for name in ('baseline', 'candidate')}
    modules = {name: load(path) for name, path in paths.items()}

    async def async_callback():
        await asyncio.sleep(0)
        return 42

    def sync_callback():
        return 42

    async def run():
        results = {}
        for label, callback in [('synchronous', sync_callback), ('async_sleep_zero', async_callback)]:
            callbacks = [callback] * args.count
            async def measure(name, mode):
                start = time.perf_counter_ns()
                values = await (reference(callbacks) if name == 'reference' else
                                getattr(modules[name], 'run_' + mode)(callbacks))
                elapsed = time.perf_counter_ns() - start
                if values != [42] * args.count:
                    raise AssertionError('incorrect callback result')
                return elapsed

            results[label] = {}
            for mode in ('direct', 'from_worker'):
                for name in ('reference', 'baseline', 'candidate'):
                    await measure(name, mode)
                samples = []
                orders = (('reference', 'baseline', 'candidate'),
                          ('candidate', 'reference', 'baseline'),
                          ('baseline', 'candidate', 'reference'))
                for index in range(args.pairs):
                    samples.append({name: await measure(name, mode) for name in orders[index % 3]})
                results[label][mode] = {
                    'samples_ns': samples,
                    'median_baseline_over_candidate': statistics.median(
                        sample['baseline'] / sample['candidate'] for sample in samples),
                    'median_reference_over_candidate': statistics.median(
                        sample['reference'] / sample['candidate'] for sample in samples),
                }
        return results

    print(json.dumps({'python': platform.python_version(), 'callback_count': args.count,
                      'pairs': args.pairs,
                      'binaries_sha256': {name: hashlib.sha256(path.read_bytes()).hexdigest()
                                          for name, path in paths.items()},
                      'scope': 'Sequential callbacks in one asyncio loop; no Jinja template or DOCX.',
                      'workloads': asyncio.run(run())}, indent=2))


if __name__ == '__main__':
    main()
