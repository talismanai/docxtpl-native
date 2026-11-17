"""Compare release session compilation and rendering across two revisions."""
import argparse
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time

import jinja2
import docxtpl_native
from docxtpl_native.bridge import BridgeConfig


def measure(case, iterations):
    environment = jinja2.Environment()
    environment.filters['mark'] = lambda value: value
    context = {'consume': lambda value: value, 'value': 'x'}
    sources = {'plain': '{{ consume(value) }}', 'dynamic': '{{ consume(value|mark) }}',
               'constant': "{{ consume('x'|mark) }}"}
    source = sources[case]
    session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
    for _ in range(50): assert session.render('bench', source) == 'x'
    start = time.perf_counter_ns()
    for _ in range(iterations):
        if session.render('bench', source) != 'x': raise AssertionError('unexpected output')
    elapsed = (time.perf_counter_ns()-start)/iterations
    return {'ns_per_render': elapsed, 'source_revision': os.environ.get('GITHUB_SHA'),
            'module': docxtpl_native.__file__, 'extension': docxtpl_native._extension().__file__}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', required=True, choices=('plain', 'dynamic', 'constant'))
    parser.add_argument('--iterations', type=int, default=2000)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--baseline-root'); parser.add_argument('--candidate-root')
    parser.add_argument('--baseline-revision'); parser.add_argument('--candidate-revision')
    args = parser.parse_args()
    if args.iterations <= 0: parser.error('iterations must be positive')
    if args.worker:
        print(json.dumps(measure(args.case, args.iterations))); return
    if not all((args.baseline_root, args.candidate_root, args.baseline_revision, args.candidate_revision)):
        parser.error('both roots and revisions are required')
    roots = {'baseline': Path(args.baseline_root).resolve(), 'candidate': Path(args.candidate_root).resolve()}
    revisions = {'baseline': args.baseline_revision, 'candidate': args.candidate_revision}
    samples = []; provenance = {}
    for index in range(9):
        pair = {}
        for role in (('baseline', 'candidate') if index % 2 == 0 else ('candidate', 'baseline')):
            env = os.environ.copy(); env['PYTHONPATH'] = str(roots[role]); env['GITHUB_SHA'] = revisions[role]
            process = subprocess.run([sys.executable, __file__, '--worker', '--case', args.case,
                                      '--iterations', str(args.iterations)], env=env, text=True,
                                     capture_output=True, check=True)
            result = json.loads(process.stdout)
            if not all(Path(result[key]).resolve().is_relative_to(roots[role]) for key in ('module', 'extension')):
                raise RuntimeError('worker imported package outside requested root: '+role)
            provenance[role] = {key: result[key] for key in ('module', 'extension')}
            pair[role] = result['ns_per_render']
        samples.append(pair)
    print(json.dumps({'baseline_revision': revisions['baseline'], 'candidate_revision': revisions['candidate'],
        'case': args.case, 'python': platform.python_version(), 'iterations': args.iterations, 'warmups': 50,
        'samples': samples, 'provenance': provenance,
        'median_baseline_over_candidate': statistics.median(x['baseline']/x['candidate'] for x in samples),
        'scope': 'Compilation and rendering of a synthetic in-memory template; no DOCX generation or visual comparison.'}, indent=2))


if __name__ == '__main__': main()
