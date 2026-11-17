"""Release generation phases, separate cProfile diagnostics, loop scaling and memory.

Run with the isolated release wheel first on PYTHONPATH. Generated DOCX files
stay outside git; only JSON evidence is retained. Comparators are unchanged.
"""
from __future__ import annotations

import argparse
import cProfile
import datetime
import gc
import hashlib
import io
import json
import os
import platform
import pstats
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

import docxtpl
import docxtpl_native
from paritylab.cases import LookupRecord, fixture, inputs
from paritylab.compare import compare_docx
from paritylab.reference_fingerprint import installed_reference

CASES = ('plain', 'long_paragraphs', 'pass_context', 'loader', 'image',
         'subdocument', 'foreign_lookup')
ROUTING_CASES = ('plain', 'plain_environment', 'plain_exposed')
ENGINES = {'reference': docxtpl, 'candidate': docxtpl_native}
PHASES = ('construct', 'inputs', 'render', 'save')
ROOT = Path(__file__).resolve().parents[1]


class Collections:
    def __init__(self):
        self.elapsed = 0
        self.counts = [0, 0, 0]
        self.started = 0

    def event(self, phase, info):
        if phase == 'start':
            self.started = time.perf_counter_ns()
        else:
            self.elapsed += time.perf_counter_ns() - self.started
            self.counts[info['generation']] += 1


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_isolation():
    competing = []
    for line in subprocess.check_output(['ps', '-eo', 'pid,comm,args'], text=True).splitlines()[1:]:
        pid, name, command = line.strip().split(None, 2)
        if int(pid) == os.getpid():
            continue
        if name in {'rustc', 'cargo', 'maturin', 'soffice.bin'} or (
            name.startswith('python') and any(marker in command for marker in (
                '-m paritylab.cli', '-m paritylab.worker', '-m unittest',
                '/bench-generation.py', '/study_performance.py'))):
            competing.append(line)
    if competing:
        raise RuntimeError('Competing measurement/build/test processes: ' + repr(competing))
    return competing


def metadata(args):
    # The measured extension is identified by its digest, recorded below with
    # the engine revision the caller declares.
    extension = Path(docxtpl_native._extension().__file__)
    reference = installed_reference()
    assert reference['matched'], reference
    return {'engine_revision': args.revision, 'mode': args.mode,
        'harness_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'harness_sha256': digest(__file__), 'harness_source': Path(__file__).read_text(),
        'extension_sha256': digest(extension), 'reference_source': reference,
        'python': platform.python_version(), 'platform': platform.platform(),
        'affinity': sorted(os.sched_getaffinity(0)), 'start_utc': now(),
        'competing_owned_jobs_at_start': check_isolation(),
        'gc_enabled': gc.isenabled(), 'gc_thresholds': gc.get_threshold(),
        'repeat': args.repeat, 'warmup': args.warmup, 'seed': args.seed,
        'scope': 'Linux shared host; fresh documents, original GC policy, fixed release wheel. '
            'Preparation of fixture files, full structural/binary comparison and diagnostic output are outside timing. '
            'Rounds shuffle workloads and alternate backend order. No visual, memory, concurrency or platform qualification. '
            'Total generation time is the sum of construct/inputs/render/save; small harness transitions between phases are excluded. '
            'GC duration is charged to the phase where collection ran, not to the allocator that created the objects. '
            'cProfile timings are instrumented diagnostics, never speedup measurements.', 'cases': []}


def timed(function, collections, profile=False):
    before_gc = collections.elapsed
    before_counts = collections.counts[:]
    profiler = cProfile.Profile() if profile else None
    if profiler:
        profiler.enable()
    wall = time.perf_counter_ns()
    cpu = time.process_time_ns()
    try:
        value = function()
    finally:
        cpu_end = time.process_time_ns()
        wall_end = time.perf_counter_ns()
        if profiler:
            profiler.disable()
    result = {'wall_ms': (wall_end - wall) / 1e6, 'cpu_ms': (cpu_end - cpu) / 1e6,
        'gc_wall_ms': (collections.elapsed - before_gc) / 1e6,
        'gc_collections': [a - b for a, b in zip(collections.counts, before_counts)]}
    if profiler:
        result['profile'] = sorted(({'file': file, 'line': line, 'function': name,
            'primitive_calls': cc, 'calls': nc, 'self_ms': tt * 1000, 'cumulative_ms': ct * 1000}
            for (file, line, name), (cc, nc, tt, ct, _) in pstats.Stats(profiler).stats.items()),
            key=lambda row: -row['self_ms'])
    return value, result


def generate(name, role, directory, template, collections, profile):
    engine = ENGINES[role]
    phases = {}
    document, phases['construct'] = timed(lambda: engine.DocxTemplate(str(template)), collections, profile)
    def prepare():
        result = inputs('plain' if name in ROUTING_CASES else name, engine, document, directory)
        if name == 'plain_environment':
            import jinja2
            result = result[0], jinja2.Environment(), result[2]
        elif name == 'plain_exposed':
            document.get_docx()
        return result
    configuration, phases['inputs'] = timed(prepare, collections, profile)
    context, environment, autoescape = configuration
    _, phases['render'] = timed(lambda: document.render(context, jinja_env=environment,
        autoescape=autoescape), collections, profile)
    stream = io.BytesIO()
    _, phases['save'] = timed(lambda: document.save(stream), collections, profile)
    execution = dict(document.last_execution) if role == 'candidate' else None
    if execution:
        assert execution['renderer'] == 'native'
    return stream.getvalue(), {'phases': phases,
        'total_wall_ms': sum(row['wall_ms'] for row in phases.values()),
        'total_cpu_ms': sum(row['cpu_ms'] for row in phases.values()),
        'gc_wall_ms': sum(row['gc_wall_ms'] for row in phases.values()), 'execution': execution}


def quantiles(values):
    values = sorted(values)
    return {'mean': statistics.mean(values), 'p50': statistics.median(values),
        'p95': statistics.quantiles(values, n=20, method='inclusive')[-1],
        'min': values[0], 'max': values[-1]}


def summarize(samples, key='total_wall_ms'):
    summary = {}
    for role in ENGINES:
        summary[role] = {key: quantiles([row[role][key] for row in samples])}
        if 'phases' in samples[0][role]:
            summary[role]['phases_mean_ms'] = {phase: {
                metric: statistics.mean(row[role]['phases'][phase][metric] for row in samples)
                for metric in ('wall_ms', 'cpu_ms', 'gc_wall_ms')} for phase in PHASES}
            summary[role]['gc_mean_ms'] = statistics.mean(row[role]['gc_wall_ms'] for row in samples)
            summary[role]['cpu_mean_ms'] = statistics.mean(row[role]['total_cpu_ms'] for row in samples)
    ratios = [row['reference'][key] / row['candidate'][key] for row in samples]
    quartiles = statistics.quantiles(ratios, n=4, method='inclusive')
    summary['paired_reference_over_candidate'] = {
        'p25': quartiles[0], 'p50': statistics.median(ratios), 'p75': quartiles[-1],
        'candidate_faster_pairs': sum(value > 1 for value in ratios), 'pairs': len(ratios)}
    return summary


def verify_pair(outputs):
    result = compare_docx(outputs['reference'], outputs['candidate'])
    assert result['equivalent'], result


def benchmark(args, report, collections):
    rng = random.Random(args.seed)
    cases = ROUTING_CASES if args.mode == 'routing' else CASES
    paths = {name: fixture('plain' if name in ROUTING_CASES else name, args.out / name) for name in cases}
    rows = {name: {'id': name, 'template_sha256': digest(paths[name]),
        'equivalent_pairs': 0, 'samples': []} for name in cases}
    cross_checks = 0
    for iteration in range(args.warmup + args.repeat):
        names = list(cases)
        rng.shuffle(names)
        documents = {}
        for position, name in enumerate(names):
            outputs, pair = {}, {'round': iteration - args.warmup, 'position': position}
            order = ('reference', 'candidate') if (iteration + cases.index(name)) % 2 else ('candidate', 'reference')
            pair['order'] = order
            for role in order:
                outputs[role], pair[role] = generate(name, role, args.out / name,
                    paths[name], collections, args.mode == 'profile')
            verify_pair(outputs)
            if args.mode == 'routing':
                documents[name] = outputs['candidate']
            rows[name]['equivalent_pairs'] += 1
            if iteration >= args.warmup:
                rows[name]['samples'].append(pair)
        if args.mode == 'routing':
            for name in ROUTING_CASES[1:]:
                assert compare_docx(documents['plain'], documents[name])['equivalent']
                cross_checks += 1
        print(f'round {iteration + 1}/{args.warmup + args.repeat}', flush=True)
    report['cases'] = list(rows.values())
    for row in report['cases']:
        row['summary'] = summarize(row['samples'])
    if args.mode == 'routing':
        report['cross_routing_equal'] = cross_checks
        report['scope'] += 'Routing holds template content/context/output constant; variants supply a default Environment or expose the model before render. Environment/model setup is measured in inputs.'


def scaling(args, report, collections):
    from docx import Document
    directory = args.out / 'scaling'
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for kind, source in (
        ('objects', '{% for item in rows %}{{ item.label }}={{ item["value"] }};{% endfor %}'),
        ('formatted', '{% for item in rows %}{{ item }};{% endfor %}')):
        document = Document()
        document.add_paragraph(source)
        paths[kind] = directory / (kind + '.docx')
        document.save(paths[kind])
    counts = (0, 10, 100, 1000, 5000)
    keys = [(count, kind) for count in counts for kind in paths]
    rows = {key: {'id': f'{key[1]}/{key[0]}', 'rows': key[0], 'representation': key[1],
        'template_sha256': digest(paths[key[1]]), 'samples': [], 'equivalent_pairs': 0} for key in keys}
    rng = random.Random(args.seed)
    cross_checks = 0
    for iteration in range(args.warmup + args.repeat):
        order_keys = keys[:]
        rng.shuffle(order_keys)
        documents = {}
        for count, kind in order_keys:
            outputs, pair = {}, {'round': iteration - args.warmup}
            order = ('reference', 'candidate') if (iteration + keys.index((count, kind))) % 2 else ('candidate', 'reference')
            pair['order'] = order
            for role in order:
                document = ENGINES[role].DocxTemplate(str(paths[kind]))
                # Both variants use an already exposed Python document graph.
                # This prevents a fast/native-package path from confounding the comparison.
                document.get_docx()
                values = [LookupRecord(index) for index in range(count)] if kind == 'objects' else [f'row={index}' for index in range(count)]
                _, measurement = timed(lambda: document.render({'rows': values}), collections)
                stream = io.BytesIO()
                document.save(stream)
                outputs[role] = stream.getvalue()
                pair[role] = {'render_wall_ms': measurement['wall_ms'],
                    'render_cpu_ms': measurement['cpu_ms'], 'gc_wall_ms': measurement['gc_wall_ms']}
                if role == 'candidate':
                    execution = dict(document.last_execution)
                    assert execution['renderer'] == 'native' and execution['document_model'] == 'python-docx/lxml'
                    pair[role]['execution'] = execution
            verify_pair(outputs)
            documents[count, kind] = outputs['candidate']
            rows[count, kind]['equivalent_pairs'] += 1
            if iteration >= args.warmup:
                rows[count, kind]['samples'].append(pair)
        for count in counts:
            comparison = compare_docx(documents[count, 'objects'], documents[count, 'formatted'])
            assert comparison['equivalent'], comparison
            cross_checks += 1
        print(f'round {iteration + 1}/{args.warmup + args.repeat}', flush=True)
    report['cases'] = list(rows.values())
    for row in report['cases']:
        row['summary'] = summarize(row['samples'], 'render_wall_ms')
    report['cross_representation_equal'] = cross_checks
    report['scope'] += ' Scaling times render only; model loading, row creation and save are excluded. '
    report['scope'] += 'Formatted rows remove two lookups and one value formatting per item; this is not a pure FFI microbenchmark or a proposal to remove object protocols.'


def max_rss_kib():
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS bytes.
    return value // 1024 if platform.system() == 'Darwin' else value


def memory_child(name, role, directory, repeat):
    """One fresh process per case and engine: peak RSS before, after the first
    generation and after `repeat` more; Python allocation peaks (tracemalloc)."""
    import tracemalloc
    path = fixture('plain' if name in ROUTING_CASES else name, directory)
    collections = Collections()
    result = {'case': name, 'role': role, 'baseline_rss_kib': max_rss_kib()}
    tracemalloc.start()
    output, _ = generate(name, role, directory, path, collections, False)
    result['first_python_peak_bytes'] = tracemalloc.get_traced_memory()[1]
    result['first_rss_kib'] = max_rss_kib()
    tracemalloc.reset_peak()
    for _ in range(repeat):
        generate(name, role, directory, path, collections, False)
    result['repeated_python_peak_bytes'] = tracemalloc.get_traced_memory()[1]
    result['repeated_python_retained_bytes'] = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    result['repeated_rss_kib'] = max_rss_kib()
    (directory / f'{role}.docx').write_bytes(output)
    print(json.dumps(result))


def memory(args, report, collections):
    del collections
    cases = list(CASES)
    rows = []
    for name in cases:
        row = {'id': name, 'repeat': args.repeat, 'samples': {}}
        directory = args.out / 'memory' / name
        directory.mkdir(parents=True, exist_ok=True)
        for role in ENGINES:
            command = [sys.executable, str(Path(__file__).resolve()), '--mode', 'memory-child', '--case', name,
                       '--role', role, '--revision', args.revision, '--out', str(directory), '--repeat', str(args.repeat)]
            completed = subprocess.run(command, check=True, capture_output=True, text=True, env=os.environ.copy())
            row['samples'][role] = json.loads(completed.stdout.strip().splitlines()[-1])
        verify_pair({role: (directory / f'{role}.docx').read_bytes() for role in ENGINES})
        row['equivalent_pairs'] = 1
        rows.append(row)
        print(f'memory {name}', flush=True)
    report['cases'] = rows
    report['scope'] += (' Memory: one fresh interpreter per case and engine; peak RSS (ru_maxrss) before generation, '
        'after the first generation and after the repeated ones, and tracemalloc peaks of Python allocations. '
        'Rust heap allocations and copied bytes are not instrumented; RSS includes the interpreter and libraries.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('benchmark', 'profile', 'scaling', 'routing', 'memory', 'memory-child'),
                        required=True)
    parser.add_argument('--case')
    parser.add_argument('--role', choices=tuple(ENGINES))
    parser.add_argument('--revision', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--repeat', type=int, default=41)
    parser.add_argument('--warmup', type=int, default=5)
    parser.add_argument('--seed', type=int, default=1701)
    args = parser.parse_args()
    if args.repeat < 2 or args.warmup < 0:
        parser.error('repeat >= 2 and warmup >= 0 required')
    args.out.mkdir(parents=True, exist_ok=True)
    if args.mode == 'memory-child':
        memory_child(args.case, args.role, args.out, args.repeat)
        return
    report = metadata(args)
    collections = Collections()
    gc.callbacks.append(collections.event)
    try:
        {'scaling': scaling, 'memory': memory}.get(args.mode, benchmark)(args, report, collections)
    finally:
        gc.callbacks.remove(collections.event)
    report.update(end_utc=now(), status='pass', equivalent_pairs=sum(row['equivalent_pairs'] for row in report['cases']))
    (args.out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: report[key] for key in ('mode', 'status', 'equivalent_pairs', 'start_utc', 'end_utc')}))


if __name__ == '__main__':
    main()
