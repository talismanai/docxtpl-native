"""Paired release benchmark of operator-heavy template and DOCX generation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time


def measure(case, iterations):
    import io
    import docxtpl_native
    import jinja2
    from docxtpl_native.bridge import BridgeConfig
    from paritylab.reference_fingerprint import installed_reference
    installed_reference()
    environment = jinja2.Environment()
    sources = {
        'scalars': '{{ a+b }}|{{ a*b }}|{{ a//b }}|{{ a>b }}|{{ a in values }}',
        'loop': '{% for value in values %}{{ (value+offset)*2 }}{% endfor %}',
        'concat': '{{ a ~ b ~ offset }}',
    }
    context = {'a': 17, 'b': 3, 'offset': 5, 'values': list(range(100))}
    if case in ('docx_loop', 'docx_concat'):
        from docx import Document
        from docxtpl import DocxTemplate as ReferenceTemplate
        from paritylab.compare import compare_docx
        template = io.BytesIO()
        document = Document()
        document.add_paragraph(sources['loop' if case == 'docx_loop' else 'concat'])
        document.save(template)
        original = template.getvalue()
        reference = ReferenceTemplate(io.BytesIO(original))
        reference.render(context, jinja_env=environment)
        ref_bytes = io.BytesIO()
        reference.save(ref_bytes)
        def generate():
            document = docxtpl_native.DocxTemplate(io.BytesIO(original))
            document.render(context, jinja_env=environment)
            output = io.BytesIO()
            document.save(output)
            return output.getvalue()
        warmups = 2
        for _ in range(warmups):
            generated = generate()
        if not compare_docx(ref_bytes.getvalue(), generated)['equivalent']:
            raise AssertionError('candidate DOCX differs structurally from reference')
        start = time.perf_counter_ns()
        for _ in range(iterations):
            generated = generate()
        elapsed = time.perf_counter_ns() - start
        if not compare_docx(ref_bytes.getvalue(), generated)['equivalent']:
            raise AssertionError('measured candidate DOCX differs structurally from reference')
    else:
        source = sources[case]
        expected = environment.from_string(source).render(context)
        session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
        warmups = 20
        for _ in range(warmups):
            if session.render('benchmark', source) != expected:
                raise AssertionError('native output differs from Jinja')
        start = time.perf_counter_ns()
        for _ in range(iterations):
            if session.render('benchmark', source) != expected:
                raise AssertionError('native output differs from Jinja')
        elapsed = time.perf_counter_ns() - start
    extension = Path(docxtpl_native._extension().__file__).resolve()
    return {'ns_per_render': elapsed / iterations, 'warmups': warmups,
            'module': str(Path(docxtpl_native.__file__).resolve()), 'extension': str(extension),
            'extension_sha256': hashlib.sha256(extension.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=('scalars', 'loop', 'docx_loop', 'concat', 'docx_concat'), required=True)
    parser.add_argument('--iterations', type=int, default=100)
    parser.add_argument('--pairs', type=int, default=9)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--baseline-root')
    parser.add_argument('--candidate-root')
    parser.add_argument('--dependency-site')
    parser.add_argument('--baseline-revision')
    parser.add_argument('--candidate-revision')
    args = parser.parse_args()
    if args.iterations < 1 or args.pairs < 1:
        parser.error('iterations and pairs must be positive')
    if args.worker:
        print(json.dumps(measure(args.case, args.iterations)))
        return
    if not all((args.baseline_root, args.candidate_root, args.dependency_site,
                args.baseline_revision, args.candidate_revision)):
        parser.error('both roots, revisions and dependency site are required')
    roots = {name: Path(getattr(args, name + '_root')).resolve() for name in ('baseline', 'candidate')}
    dependency_site = Path(args.dependency_site).resolve()
    samples, provenance = [], {}
    for index in range(args.pairs):
        pair = {}
        order = ('baseline', 'candidate') if index % 2 == 0 else ('candidate', 'baseline')
        for name in order:
            environment = os.environ.copy()
            environment['PYTHONPATH'] = os.pathsep.join((str(roots[name]), str(dependency_site)))
            process = subprocess.run([sys.executable, __file__, '--worker', '--case', args.case,
                                      '--iterations', str(args.iterations)], env=environment,
                                     text=True, capture_output=True, check=True)
            result = json.loads(process.stdout)
            if not all(Path(result[key]).is_relative_to(roots[name]) for key in ('module', 'extension')):
                raise RuntimeError('worker imported package outside requested root: ' + name)
            identity = {key: result[key] for key in ('module', 'extension', 'extension_sha256')}
            if name in provenance and provenance[name] != identity:
                raise RuntimeError('worker binary changed during benchmark: ' + name)
            provenance[name] = identity
            pair[name] = result['ns_per_render']
        samples.append(pair)
    print(json.dumps({'baseline_revision': args.baseline_revision,
        'candidate_revision': args.candidate_revision, 'case': args.case,
        'python': platform.python_version(), 'iterations': args.iterations, 'warmups': 2 if args.case in ('docx_loop', 'docx_concat') else 20,
        'samples': samples, 'provenance': provenance,
        'median_baseline_over_candidate': statistics.median(x['baseline'] / x['candidate'] for x in samples),
        'scope': ('Synthetic in-memory DOCX loading, rendering and saving; validation outside timing; no visual comparison.'
                  if args.case in ('docx_loop', 'docx_concat') else 'Compilation and rendering of synthetic in-memory templates; no DOCX or visual comparison.')}, indent=2))


if __name__ == '__main__':
    main()
