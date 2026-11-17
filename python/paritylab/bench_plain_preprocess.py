"""Compare Python Jinja and Rust session preprocessing without DOCX I/O."""
import argparse
import json
import os
import platform
import statistics
import time

import jinja2
from jinja2.ext import Extension
import docxtpl_native
from docxtpl_native.bridge import BridgeConfig


class Replace(Extension):
    def preprocess(self, source, name, filename=None):
        return source.replace('ORIGINAL_TOKEN','replacement')


def measure(iterations):
    source='<w:p>'+('plain text ORIGINAL_TOKEN '*500)+'</w:p>'
    expected=source.replace('ORIGINAL_TOKEN','replacement')
    reference=jinja2.Environment(extensions=[Replace])
    candidate=jinja2.Environment(extensions=[Replace])
    session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(candidate,False),{})
    def old():return reference.from_string(source).render({})
    def current():return session.render('bench',source)
    for _ in range(50):assert old()==expected and current()==expected
    samples=[]
    for index in range(9):
        pair={}
        for name in (('reference','native') if index%2==0 else ('native','reference')):
            call=old if name=='reference' else current
            start=time.perf_counter_ns()
            for _ in range(iterations):
                if call()!=expected:raise AssertionError('unexpected output')
            pair[name]=(time.perf_counter_ns()-start)/iterations
        samples.append(pair)
    return {'source_revision':os.environ.get('GITHUB_SHA'),'python':platform.python_version(),
        'iterations':iterations,'warmups':50,'samples':samples,
        'median_reference_over_native':statistics.median(s['reference']/s['native'] for s in samples),
        'extension':docxtpl_native._extension().__file__,
        'scope':'Python Jinja compilation/render versus Rust plain-text session with a user preprocess hook; no DOCX I/O.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--iterations',type=int,default=2000)
    args=parser.parse_args()
    if args.iterations<=0:parser.error('iterations must be positive')
    print(json.dumps(measure(args.iterations),indent=2))
