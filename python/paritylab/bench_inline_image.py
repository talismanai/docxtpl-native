"""Compare InlineImage string conversion with a minimal document model."""
import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

import docxtpl
import docxtpl_native


class Picture:
    xml = '<picture/>'


class Part:
    def new_pic_inline(self, source, width, height):
        return Picture()


class Template:
    current_rendering_part = Part()


def measure(count, iterations, warmups):
    template = Template()
    samples = []
    classes = {'reference': docxtpl.InlineImage, 'native': docxtpl_native.InlineImage}
    images = {name: cls(template, 'source') for name, cls in classes.items()}
    assert str(images['reference']) == str(images['native'])
    for _ in range(warmups):
        for image in images.values():
            str(image)
    for index in range(count):
        measurement = {}
        for name in (('reference', 'native') if index % 2 == 0 else ('native', 'reference')):
            start = time.perf_counter_ns()
            for _ in range(iterations):
                str(images[name])
            measurement[name] = (time.perf_counter_ns() - start) / iterations
        samples.append(measurement)
    return {'source_revision': os.environ.get('GITHUB_SHA'),
            'python': platform.python_version(), 'iterations': iterations,
            'warmups': warmups, 'samples': samples,
            'module': docxtpl_native.__file__, 'extension': docxtpl_native._extension().__file__,
            'median_reference_over_native': statistics.median(
                sample['reference'] / sample['native'] for sample in samples),
            'scope': 'InlineImage string conversion with an in-memory stub; excludes document generation.'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--baseline-root')
    parser.add_argument('--candidate-root')
    parser.add_argument('--baseline-revision')
    parser.add_argument('--candidate-revision')
    parser.add_argument('--iterations',type=int,default=200000)
    parser.add_argument('--warmups',type=int,default=1000)
    args=parser.parse_args()
    if args.iterations <= 0 or args.warmups < 0:
        parser.error('iterations must be positive and warmups must be nonnegative')
    if args.worker:
        print(json.dumps(measure(1,args.iterations,args.warmups)));return
    if not any((args.baseline_root,args.candidate_root,args.baseline_revision,args.candidate_revision)):
        print(json.dumps(measure(9,args.iterations,args.warmups),indent=2));return
    if not all((args.baseline_root,args.candidate_root,args.baseline_revision,args.candidate_revision)):
        parser.error('both roots and revisions are required')
    roots={'baseline':Path(args.baseline_root).resolve(),'candidate':Path(args.candidate_root).resolve()}
    revisions={'baseline':args.baseline_revision,'candidate':args.candidate_revision}
    pairs=[];provenance={}
    for index in range(9):
        pair={}
        for role in (('baseline','candidate') if index%2==0 else ('candidate','baseline')):
            env=os.environ.copy();env['PYTHONPATH']=str(roots[role]);env['GITHUB_SHA']=revisions[role]
            result=subprocess.run([sys.executable,__file__,'--worker','--iterations',str(args.iterations),
                                   '--warmups',str(args.warmups)],env=env,capture_output=True,text=True,check=True)
            observed=json.loads(result.stdout)
            if not all(Path(observed[key]).resolve().is_relative_to(roots[role]) for key in ('module','extension')):
                raise RuntimeError('worker imported a package outside its requested root: '+role)
            provenance[role]={key:observed[key] for key in ('module','extension')}
            pair[role]=observed['samples'][0]
        pairs.append(pair)
    print(json.dumps({'baseline_revision':revisions['baseline'],'candidate_revision':revisions['candidate'],
        'python':platform.python_version(),'iterations':args.iterations,'warmups':args.warmups,'samples':pairs,'provenance':provenance,
        'median_baseline_over_candidate':statistics.median(p['baseline']['native']/p['candidate']['native'] for p in pairs),
        'median_reference_over_candidate':statistics.median(p['candidate']['reference']/p['candidate']['native'] for p in pairs),
        'scope':'InlineImage string conversion with an in-memory stub; excludes document generation.'},indent=2))


if __name__ == '__main__':
    main()
