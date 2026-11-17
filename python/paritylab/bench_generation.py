"""Interleaved DOCX generation benchmark; fixtures/comparison are not timed."""
import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import statistics
import time
from pathlib import Path
import docxtpl
import docxtpl_native
from paritylab.cases import CASES, fixture, inputs
from paritylab.compare import compare_docx
from paritylab.reference_fingerprint import installed_reference


def measure(args):
    root = Path(args.out).resolve()
    root.mkdir(parents=True, exist_ok=True)
    extension = Path(docxtpl_native._extension().__file__)
    report = {'source_revision': os.environ.get('GITHUB_SHA'),
              'python': platform.python_version(), 'platform': platform.platform(),
              'processor': platform.processor(), 'affinity': sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None,
              'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'extension_sha256': hashlib.sha256(extension.read_bytes()).hexdigest(),
              'reference_source': installed_reference(),
              'dependencies': {name: importlib.metadata.version(name) for name in
                               ('Jinja2', 'python-docx', 'lxml', 'docxcompose')},
              'repeat': args.repeat, 'warmup': args.warmup,
              'scope': 'Fresh DocxTemplate per generation; render and save measured separately. Fixture generation, imports, structural/binary comparison and file writes are outside timing. Backends alternate each pair. Shared host; no universal speedup, memory or platform qualification.',
              'cases': []}
    engines = {'reference': docxtpl, 'candidate': docxtpl_native}
    for name in args.case:
        directory = root / name
        template = fixture(name, directory)
        samples = []
        for index in range(args.repeat + args.warmup):
            pair = {}
            outputs = {}
            for role in (('reference', 'candidate') if index % 2 == 0 else ('candidate', 'reference')):
                lib = engines[role]
                start = time.perf_counter_ns()
                cpu = time.process_time_ns()
                document = lib.DocxTemplate(str(template))
                context, environment, autoescape = inputs(name, lib, document, directory)
                render = time.perf_counter_ns()
                document.render(context, jinja_env=environment, autoescape=autoescape)
                save = time.perf_counter_ns()
                stream = io.BytesIO()
                document.save(stream)
                end = time.perf_counter_ns()
                cpu_end = time.process_time_ns()
                outputs[role] = stream.getvalue()
                pair[role] = {'total_ms': (end-start)/1e6, 'cpu_ms': (cpu_end-cpu)/1e6,
                              'render_ms': (save-render)/1e6, 'save_ms': (end-save)/1e6,
                              'bytes': len(outputs[role])}
                if role == 'candidate':
                    assert document.last_execution['renderer'] == 'native'
            comparison = compare_docx(outputs['reference'], outputs['candidate'])
            if not comparison['equivalent']:
                raise AssertionError((name, comparison))
            if index >= args.warmup:
                samples.append(pair)
            if index == args.repeat + args.warmup - 1:
                for role, data in outputs.items():
                    (directory / (role + '.docx')).write_bytes(data)
        result = {'id': name, 'equivalent_pairs': args.repeat + args.warmup,
                  'template_sha256': hashlib.sha256(template.read_bytes()).hexdigest(),
                  'samples': samples, 'summary': {}}
        for role in engines:
            values = [pair[role]['total_ms'] for pair in samples]
            result['summary'][role] = {'p50_total_ms': statistics.median(values),
                                      'p95_total_ms': statistics.quantiles(values, n=20, method='inclusive')[-1],
                                      'min_total_ms': min(values), 'max_total_ms': max(values),
                                      'p50_render_ms': statistics.median(pair[role]['render_ms'] for pair in samples),
                                      'p50_save_ms': statistics.median(pair[role]['save_ms'] for pair in samples)}
        result['median_paired_reference_over_candidate'] = statistics.median(
            pair['reference']['total_ms']/pair['candidate']['total_ms'] for pair in samples)
        report['cases'].append(result)
        (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=sorted(CASES), action='append')
    parser.add_argument('--repeat', type=int, default=21)
    parser.add_argument('--warmup', type=int, default=3)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    if args.repeat < 2 or args.warmup < 0:
        parser.error('repeat >= 2 and warmup >= 0 required')
    if not args.case:
        args.case = ['plain', 'long_paragraphs', 'pass_context', 'loader', 'image', 'subdocument']
    print(json.dumps(measure(args), ensure_ascii=False, indent=2))
