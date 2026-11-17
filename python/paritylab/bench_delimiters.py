"""Paired native part-render benchmark for delimiter restoration."""
import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path


def worker(case,iterations):
    import docxtpl_native
    value={'plain':'plain text '*1500,'markers':'{_{word}_} '*1500,
           'overlap':'prefix{_{_%suffix '*1000}[case]
    expected=value
    for marker,replacement in (('{_{','{{'),('}_}','}}'),('{_%','{%'),('%_}','%}')):
        expected=expected.replace(marker,replacement)
    expected='<w:p>'+expected+'</w:p>'
    source='<w:p>{{ value }}</w:p>'
    document=docxtpl_native.DocxTemplate(None);part=object();context={'value':value}
    for _ in range(3):assert document.render_xml_part(source,part,context)==expected
    start=time.perf_counter_ns()
    for _ in range(iterations):
        result=document.render_xml_part(source,part,context)
        assert result==expected
    return {'ns_per_render':(time.perf_counter_ns()-start)/iterations,
            'sha256':hashlib.sha256(expected.encode()).hexdigest(),
            'module':docxtpl_native.__file__,'extension':docxtpl_native._extension().__file__}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--case',choices=('plain','markers','overlap'),required=True)
    parser.add_argument('--iterations',type=int,default=500)
    parser.add_argument('--baseline-root');parser.add_argument('--candidate-root')
    parser.add_argument('--baseline-revision');parser.add_argument('--candidate-revision')
    args=parser.parse_args()
    if args.iterations<=0:parser.error('iterations must be positive')
    if args.worker:print(json.dumps(worker(args.case,args.iterations)));return
    if not all((args.baseline_root,args.candidate_root,args.baseline_revision,args.candidate_revision)):
        parser.error('both roots and revisions are required')
    roots={'baseline':Path(args.baseline_root).resolve(),'candidate':Path(args.candidate_root).resolve()}
    samples=[];provenance={};digest=None
    for index in range(9):
        pair={}
        for role in (('baseline','candidate') if index%2==0 else ('candidate','baseline')):
            env=os.environ.copy();env['PYTHONPATH']=str(roots[role])
            result=subprocess.run([sys.executable,__file__,'--worker','--case',args.case,
                                   '--iterations',str(args.iterations)],env=env,capture_output=True,text=True,check=True)
            observed=json.loads(result.stdout)
            if not all(Path(observed[key]).resolve().is_relative_to(roots[role]) for key in ('module','extension')):
                raise RuntimeError('worker imported a package outside its requested root: '+role)
            if digest is not None and digest!=observed['sha256']:raise RuntimeError('output differs')
            digest=observed['sha256'];provenance[role]={key:observed[key] for key in ('module','extension')}
            pair[role]=observed['ns_per_render']
        samples.append(pair)
    print(json.dumps({'baseline_revision':args.baseline_revision,'candidate_revision':args.candidate_revision,
        'case':args.case,'python':platform.python_version(),'iterations':args.iterations,'warmups':3,
        'samples':samples,'provenance':provenance,'sha256':digest,
        'median_baseline_over_candidate':statistics.median(s['baseline']/s['candidate'] for s in samples),
        'scope':'Native render_xml_part with 16 KB text; compilation and rendering included; no DOCX.'},indent=2))


if __name__=='__main__':main()
