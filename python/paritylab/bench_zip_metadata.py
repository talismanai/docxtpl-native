"""Paired generation benchmark for the ZIP metadata compatibility change."""
import argparse
import io
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import time
import zipfile

from docx import Document
import docxtpl_native


def measure(iterations, warmups, old_timestamps, image_side):
    document=Document();document.add_paragraph('Café {{ name }}')
    if image_side:
        from PIL import Image
        pixels=random.Random(931).randbytes(image_side*image_side*3)
        image=Image.frombytes('RGB',(image_side,image_side),pixels);buffer=io.BytesIO()
        image.save(buffer,format='PNG');buffer.seek(0);document.add_picture(buffer)
    source=io.BytesIO();document.save(source);template=source.getvalue()
    if old_timestamps:
        from paritylab.embedded_probes import fixed_archive
        template=fixed_archive(template,zipfile.ZIP_DEFLATED)
    def generate():
        item=docxtpl_native.DocxTemplate(io.BytesIO(template));item.render({'name':'Ada'})
        output=io.BytesIO();item.save(output);return output.getvalue()
    for _ in range(warmups):generate()
    started=time.perf_counter_ns()
    for _ in range(iterations):result=generate()
    duration=(time.perf_counter_ns()-started)/iterations
    observed=Document(io.BytesIO(result))
    if observed.paragraphs[0].text!='Café Ada' or len(observed.inline_shapes)!=int(bool(image_side)):
        raise AssertionError('unexpected output')
    return {'ns_per_document':duration,'module':docxtpl_native.__file__,
            'extension':docxtpl_native._extension().__file__}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--baseline-root');parser.add_argument('--candidate-root')
    parser.add_argument('--baseline-revision');parser.add_argument('--candidate-revision')
    parser.add_argument('--iterations',type=int,default=20);parser.add_argument('--warmups',type=int,default=3)
    parser.add_argument('--old-timestamps',action='store_true')
    parser.add_argument('--image-side',type=int,default=0,help='side of deterministic RGB PNG; 0 disables it')
    args=parser.parse_args()
    if args.iterations<=0 or args.warmups<0 or args.image_side<0:parser.error('invalid benchmark argument')
    if args.worker:
        print(json.dumps(measure(args.iterations,args.warmups,args.old_timestamps,args.image_side)));return
    if not all((args.baseline_root,args.candidate_root,args.baseline_revision,args.candidate_revision)):
        parser.error('both roots and revisions are required')
    roots={'baseline':Path(args.baseline_root).resolve(),'candidate':Path(args.candidate_root).resolve()}
    revisions={'baseline':args.baseline_revision,'candidate':args.candidate_revision}
    samples=[];provenance={}
    for index in range(9):
        pair={}
        for role in (('baseline','candidate') if index%2==0 else ('candidate','baseline')):
            env=os.environ.copy();env['PYTHONPATH']=str(roots[role]);env['GITHUB_SHA']=revisions[role]
            command=[sys.executable,__file__,'--worker','--iterations',str(args.iterations),
                     '--warmups',str(args.warmups),'--image-side',str(args.image_side)]
            if args.old_timestamps:command.append('--old-timestamps')
            result=subprocess.run(command,env=env,text=True,capture_output=True,check=True)
            observed=json.loads(result.stdout)
            if not all(Path(observed[key]).resolve().is_relative_to(roots[role]) for key in ('module','extension')):
                raise RuntimeError('worker imported package outside requested root: '+role)
            provenance[role]={key:observed[key] for key in ('module','extension')}
            pair[role]=observed['ns_per_document']
        samples.append(pair)
    print(json.dumps({'baseline_revision':revisions['baseline'],'candidate_revision':revisions['candidate'],
        'python':platform.python_version(),'iterations':args.iterations,'warmups':args.warmups,
        'old_timestamps':args.old_timestamps,
        'image_side':args.image_side,
        'samples':samples,'provenance':provenance,
        'median_baseline_over_candidate':statistics.median(p['baseline']/p['candidate'] for p in samples),
        'scope':'Complete in-memory DOCX render and save; output validation is outside timing.'},indent=2))


if __name__=='__main__':main()
