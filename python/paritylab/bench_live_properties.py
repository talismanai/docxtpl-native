"""Compare the previous and native exposed-property loops with stub callbacks."""
import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

import docxtpl_native


class Properties:
    author=comments=identifier=language=subject=title='value'


class Model:
    core_properties=Properties()


class Document:
    docx=Model()


class Session:
    def render(self,name,value):return value


def previous(document,session):
    for name in ('author','comments','identifier','language','subject','title'):
        properties=document.docx.core_properties
        initial=getattr(properties,name)
        rendered=session.render('<string>',initial)
        setattr(properties,name,rendered)


def measure(count,iterations,warmups):
    native=docxtpl_native._extension().render_live_properties
    document=Document();session=Session()
    def current():return native(document,session,'<string>')
    def old():return previous(document,session)
    for _ in range(warmups):old();current()
    assert all(getattr(document.docx.core_properties,name)=='value'
               for name in ('author','comments','identifier','language','subject','title'))
    samples=[]
    for index in range(count):
        pair={}
        for name in (('previous','native') if index%2==0 else ('native','previous')):
            function=old if name=='previous' else current
            start=time.perf_counter_ns()
            for _ in range(iterations):function()
            pair[name]=(time.perf_counter_ns()-start)/iterations
        samples.append(pair)
    assert all(getattr(document.docx.core_properties,name)=='value'
               for name in ('author','comments','identifier','language','subject','title'))
    return {'source_revision':os.environ.get('GITHUB_SHA'),'python':platform.python_version(),
        'iterations':iterations,'warmups':warmups,'samples':samples,
        'module':docxtpl_native.__file__,'extension':docxtpl_native._extension().__file__,
        'median_previous_over_native':statistics.median(s['previous']/s['native'] for s in samples),
        'scope':'Six property transforms with stub document and callback; no template compilation or DOCX IO.'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--baseline-root');parser.add_argument('--candidate-root')
    parser.add_argument('--baseline-revision');parser.add_argument('--candidate-revision')
    parser.add_argument('--iterations',type=int,default=100000);parser.add_argument('--warmups',type=int,default=1000)
    args=parser.parse_args()
    if args.iterations<=0 or args.warmups<0:parser.error('invalid iteration or warmup count')
    if args.worker:print(json.dumps(measure(1,args.iterations,args.warmups)));return
    if not any((args.baseline_root,args.candidate_root,args.baseline_revision,args.candidate_revision)):
        print(json.dumps(measure(9,args.iterations,args.warmups),indent=2));return
    if not all((args.baseline_root,args.candidate_root,args.baseline_revision,args.candidate_revision)):
        parser.error('both roots and revisions are required')
    roots={'baseline':Path(args.baseline_root).resolve(),'candidate':Path(args.candidate_root).resolve()}
    revisions={'baseline':args.baseline_revision,'candidate':args.candidate_revision}
    samples=[];provenance={}
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
        samples.append(pair)
    print(json.dumps({'baseline_revision':revisions['baseline'],'candidate_revision':revisions['candidate'],
        'python':platform.python_version(),'iterations':args.iterations,'warmups':args.warmups,
        'samples':samples,'provenance':provenance,
        'median_baseline_over_candidate':statistics.median(s['baseline']['native']/s['candidate']['native'] for s in samples),
        'median_previous_over_candidate':statistics.median(s['candidate']['previous']/s['candidate']['native'] for s in samples),
        'scope':'Six property transforms with stub document and callback; no template compilation or DOCX IO.'},indent=2))


if __name__=='__main__':main()
