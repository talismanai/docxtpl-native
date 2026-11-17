"""Paired iteration microbenchmark; no template execution or DOCX."""
import argparse
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import statistics
import time

from jinja2.async_utils import auto_aiter


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--extension', type=Path, required=True)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--count', type=int, default=200)
    parser.add_argument('--pairs', type=int, default=21)
    args = parser.parse_args()
    if args.count < 1 or args.pairs < 1:
        parser.error('count and pairs must be positive')
    path = args.extension.resolve()
    spec = importlib.util.spec_from_file_location('_async_direct', str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    baseline = None
    if args.baseline:
        baseline_spec = importlib.util.spec_from_file_location('_async_direct', str(args.baseline.resolve()))
        baseline = importlib.util.module_from_spec(baseline_spec)
        baseline_spec.loader.exec_module(baseline)

    async def run():
        results = {}
        for kind in ('sync', 'async_immediate', 'async_sleep_zero'):
            def source():
                if kind == 'sync':
                    return iter(range(args.count))
                async def values():
                    for number in range(args.count):
                        if kind == 'async_sleep_zero':
                            await asyncio.sleep(0)
                        yield number
                return values()

            async def measure(mode):
                iterable = source()
                start = time.perf_counter_ns()
                if mode == 'reference':
                    values = [value async for value in auto_aiter(iterable)]
                else:
                    values = await (baseline if mode == 'baseline' else module).collect_iteration(iterable)
                elapsed = time.perf_counter_ns() - start
                if values != list(range(args.count)):
                    raise AssertionError('wrong values')
                return elapsed

            modes = ('reference', 'baseline', 'candidate') if baseline else ('reference', 'candidate')
            for mode in modes:
                await measure(mode)
            samples = []
            for index in range(args.pairs):
                offset = index % len(modes)
                order = modes[offset:] + modes[:offset]
                samples.append({mode: await measure(mode) for mode in order})
            results[kind] = {'samples_ns': samples,
                'median_reference_over_candidate': statistics.median(
                    sample['reference'] / sample['candidate'] for sample in samples)}
            if baseline:
                results[kind]['median_baseline_over_candidate'] = statistics.median(
                    sample['baseline'] / sample['candidate'] for sample in samples)
        return results

    print(json.dumps({'python': platform.python_version(), 'count': args.count,
        'pairs': args.pairs, 'extension_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'baseline_sha256': hashlib.sha256(args.baseline.read_bytes()).hexdigest() if args.baseline else None,
        'scope': 'Collecting one iterable in a single asyncio task; no Jinja template or DOCX.',
        'workloads': asyncio.run(run())}, indent=2))


if __name__ == '__main__':
    main()
