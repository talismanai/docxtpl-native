"""Paired benchmark of a filtered loop with else, excluding DOCX I/O."""
import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path


def worker(case):
    import docxtpl_native
    import jinja2
    from docxtpl_native import _extension
    from docxtpl_native.bridge import BridgeConfig
    native=_extension()
    expected='x'*500
    environment=None
    if case in ('metadata_last','metadata_length','no_metadata'):
        values=list(range(1000));environment=jinja2.Environment()
        environment.tests['selected']=lambda value:value%2==1
        if case=='metadata_last':
            body="x{% if loop.last %}L{% endif %}";expected+='L'
        elif case=='metadata_length':body="x{% if loop.length == 500 %}{% endif %}"
        else:body='x'
        source='{% for value in values if value is selected %}'+body+'{% endfor %}'
    elif case=='filtered_unpack':
        values=[(number,'x') for number in range(1000)]
        source='{% for number, letter in values if number % 2 %}{{ letter }}{% else %}E{% endfor %}'
    else:
        values=list(range(1000))
        source='{% for value in values if value % 2 %}x{% else %}E{% endfor %}'
    session=native.PythonRenderSession(BridgeConfig(environment,False),{'values':values})
    for _ in range(3):assert session.render('bench',source)==expected
    start=time.perf_counter_ns()
    for _ in range(20):assert session.render('bench',source)==expected
    return {'ns_per_render':(time.perf_counter_ns()-start)/20,
            'module':docxtpl_native.__file__,'extension':native.__file__}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--case',choices=['filtered_else','filtered_unpack','metadata_last','metadata_length','no_metadata'],default='filtered_else')
    parser.add_argument('--baseline-root')
    parser.add_argument('--candidate-root')
    parser.add_argument('--baseline-revision')
    parser.add_argument('--candidate-revision')
    args=parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.case)));return
    if not all([args.baseline_root,args.candidate_root,args.baseline_revision,args.candidate_revision]):
        parser.error('both roots and revisions are required')
    paths={'baseline':args.baseline_root,'candidate':args.candidate_root}
    samples=[];provenance={}
    for index in range(9):
        times={}
        for role in (('baseline','candidate') if index%2==0 else ('candidate','baseline')):
            environment=os.environ.copy();environment['PYTHONPATH']=str(Path(paths[role]).resolve())
            completed=subprocess.run([sys.executable,__file__,'--worker','--case',args.case],env=environment,
                                     check=True,capture_output=True,text=True)
            observation=json.loads(completed.stdout)
            root=Path(paths[role]).resolve()
            if not all(Path(observation[key]).resolve().is_relative_to(root) for key in ('module','extension')):
                raise RuntimeError('worker imported a package outside its requested root: '+role)
            provenance[role]={key:observation[key] for key in ('module','extension')}
            times[role]=observation['ns_per_render']
        samples.append(times)
    print(json.dumps({'baseline_revision':args.baseline_revision,'candidate_revision':args.candidate_revision,
        'case':args.case,'python':platform.python_version(),'iterations':20,'warmups':3,'samples':samples,'provenance':provenance,
        'median_baseline_over_candidate':statistics.median(x['baseline']/x['candidate'] for x in samples),
        'scope':'Native session compilation and render of 1000 items; identical output; excludes DOCX and visual comparison.'},indent=2))


if __name__=='__main__':main()
