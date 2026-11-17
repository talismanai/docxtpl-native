"""Compare the old and native live-footnote traversal with stub objects."""
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

NOTE='application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml'


class Part:
    def __init__(self,content):self.content_type=content;self._blob=b'footnote'
    @property
    def blob(self):return self._blob


class Package:
    def __init__(self,parts):self.parts=parts


class SectionPart:
    def __init__(self,package):self.package=package


class Section:
    def __init__(self,part):self.part=part


class Model:
    def __init__(self,sections):self.sections=sections


class Document:
    def __init__(self,model):self.docx=model


def previous(document,render_xml):
    for section in document.docx.sections:
        for part in section.part.package.parts:
            if part.content_type=='application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml':
                source=part.blob.decode('utf-8') if isinstance(part.blob,bytes) else part.blob
                part._blob=render_xml(source,part).encode('utf-8')


def measure(case,iterations,count,model):
    if model=='stub':
        parts=[Part('other') for _ in range(9)]
        if case=='one_note':parts.append(Part(NOTE))
        section_part=SectionPart(Package(parts));document=Document(Model([Section(section_part),Section(section_part)]))
        watched=parts
    else:
        from docx import Document as NewDocument
        from docx.opc.packuri import PackURI
        from docx.opc.part import Part as OpcPart
        model_document=NewDocument();model_document.add_section()
        document=Document(model_document);watched=[]
        if case=='one_note':
            note=OpcPart(PackURI('/word/footnotes.xml'),NOTE,b'footnote',model_document.part.package)
            model_document.part.relate_to(note,'http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes')
            watched.append(note)
    def render(source,part):return source
    native=docxtpl_native._extension().render_live_footnotes
    def current():return native(document,render,NOTE,'utf-8')
    def old():return previous(document,render)
    for _ in range(1000):old();current()
    assert all(part._blob==b'footnote' for part in watched)
    samples=[]
    for index in range(count):
        pair={}
        for name in (('previous','native') if index%2==0 else ('native','previous')):
            function=old if name=='previous' else current
            start=time.perf_counter_ns()
            for _ in range(iterations):function()
            pair[name]=(time.perf_counter_ns()-start)/iterations
        samples.append(pair)
    assert all(part._blob==b'footnote' for part in watched)
    return {'source_revision':os.environ.get('GITHUB_SHA'),'python':platform.python_version(),
        'case':case,'model':model,'iterations':iterations,'warmups':1000,'samples':samples,
        'module':docxtpl_native.__file__,'extension':docxtpl_native._extension().__file__,
        'median_previous_over_native':statistics.median(s['previous']/s['native'] for s in samples),
        'scope':f'Live footnote traversal of two sections and {model} model; no template compilation or DOCX IO.'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--case',choices=('no_note','one_note'),required=True)
    parser.add_argument('--model',choices=('stub','docx'),default='stub')
    parser.add_argument('--iterations',type=int,default=100000)
    parser.add_argument('--baseline-root');parser.add_argument('--candidate-root')
    parser.add_argument('--baseline-revision');parser.add_argument('--candidate-revision')
    args=parser.parse_args()
    if args.iterations<=0:parser.error('iterations must be positive')
    if args.worker:print(json.dumps(measure(args.case,args.iterations,1,args.model)));return
    if not any((args.baseline_root,args.candidate_root,args.baseline_revision,args.candidate_revision)):
        print(json.dumps(measure(args.case,args.iterations,9,args.model),indent=2));return
    if not all((args.baseline_root,args.candidate_root,args.baseline_revision,args.candidate_revision)):
        parser.error('both roots and revisions are required')
    roots={'baseline':Path(args.baseline_root).resolve(),'candidate':Path(args.candidate_root).resolve()}
    revisions={'baseline':args.baseline_revision,'candidate':args.candidate_revision}
    samples=[];provenance={}
    for index in range(9):
        pair={}
        for role in (('baseline','candidate') if index%2==0 else ('candidate','baseline')):
            env=os.environ.copy();env['PYTHONPATH']=str(roots[role]);env['GITHUB_SHA']=revisions[role]
            result=subprocess.run([sys.executable,__file__,'--worker','--case',args.case,'--model',args.model,
                                   '--iterations',str(args.iterations)],env=env,capture_output=True,text=True,check=True)
            observed=json.loads(result.stdout)
            if not all(Path(observed[key]).resolve().is_relative_to(roots[role]) for key in ('module','extension')):
                raise RuntimeError('worker imported a package outside its requested root: '+role)
            provenance[role]={key:observed[key] for key in ('module','extension')}
            pair[role]=observed['samples'][0]
        samples.append(pair)
    print(json.dumps({'baseline_revision':revisions['baseline'],'candidate_revision':revisions['candidate'],
        'case':args.case,'model':args.model,'python':platform.python_version(),'iterations':args.iterations,'warmups':1000,
        'samples':samples,'provenance':provenance,
        'median_baseline_over_candidate':statistics.median(s['baseline']['native']/s['candidate']['native'] for s in samples),
        'median_previous_over_candidate':statistics.median(s['candidate']['previous']/s['candidate']['native'] for s in samples),
        'scope':f'Live footnote traversal of two sections and {args.model} model; no template compilation or DOCX IO.'},indent=2))


if __name__=='__main__':main()
