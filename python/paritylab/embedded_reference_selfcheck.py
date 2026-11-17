"""Check whether the pinned embedded example is byte-stable against itself."""
import argparse
import io
import json
import os
import platform
import tempfile
import time
import zipfile
from pathlib import Path

from .cli import _worker
from .compare import compare_docx
from . import visual


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream',default='.upstream/docxtpl')
    parser.add_argument('--out',required=True)
    parser.add_argument('--delay',type=float,default=3)
    parser.add_argument('--visual',action='store_true')
    args=parser.parse_args()
    if args.delay<0:parser.error('delay must be nonnegative')
    tests=(Path(args.upstream).resolve()/'tests')
    if not (tests/'embedded.py').is_file():parser.error('official embedded.py is missing')
    report={'source_revision':os.environ.get('GITHUB_SHA'), 'python':platform.python_version(),
            'separation_seconds':args.delay,'reference':'docxtpl',
            'scope':'two independent runs of the pinned official example',
            'outputs':{}}
    with tempfile.TemporaryDirectory(prefix='embedded-reference-') as folder:
        root=Path(folder); results=[]
        for index in range(2):
            out=root/str(index);out.mkdir()
            request={'kind':'upstream','backend':'docxtpl','script':'embedded.py',
                     'upstream_tests':str(tests),'output':str(out),'repeat':1,'warmup':0}
            result=_worker(request,out)
            if result['status']!='ok':raise RuntimeError(str(result.get('exception')))
            results.append(result)
            if index==0:time.sleep(args.delay)
        if set(results[0]['outputs'])!=set(results[1]['outputs']):
            raise RuntimeError('reference runs produced different output sets')
        report['reference_version']=results[0].get('engine_version')
        report['dependencies']=results[0].get('dependencies')
        for name in sorted(results[0]['outputs']):
            left=root/'0'/'documents'/name;right=root/'1'/'documents'/name
            a=left.read_bytes();b=right.read_bytes()
            report['outputs'][name]={'byte_equal':a==b,'comparison':compare_docx(a,b)}
            if args.visual:
                if not visual.available():raise RuntimeError('visual renderer unavailable')
                report['outputs'][name]['visual']=visual.compare_visual(left,right,root/'visual'/name)
        name='embedded_embedded_docx.docx'
        a=(root/'0'/'documents'/name).read_bytes();b=(root/'1'/'documents'/name).read_bytes()
        with zipfile.ZipFile(io.BytesIO(a)) as left,zipfile.ZipFile(io.BytesIO(b)) as right:
            names=left.namelist()
            if names!=right.namelist():raise RuntimeError('nested DOCX part lists differ')
            report['inner_archive']={'parts':len(names),
                'contents_equal':sum(left.read(n)==right.read(n) for n in names),
                'timestamps_equal':sum(left.getinfo(n).date_time==right.getinfo(n).date_time for n in names),
                'attributes_equal':sum(left.getinfo(n).external_attr==right.getinfo(n).external_attr for n in names)}
        embedded='word/embeddings/Document_Microsoft_Office_Word2.docx'
        report['embedded_matches_generated']=[]
        for index,inner in enumerate((a,b)):
            outer=(root/str(index)/'documents'/'embedded.docx').read_bytes()
            with zipfile.ZipFile(io.BytesIO(outer)) as archive:
                report['embedded_matches_generated'].append(archive.read(embedded)==inner)
    out=Path(args.out);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'outputs':{k:{'byte_equal':v['byte_equal'],'equivalent':v['comparison']['equivalent'],
                                  'visual_equal':v.get('visual',{}).get('equal')} for k,v in report['outputs'].items()},
                      'inner_archive':report['inner_archive'],
                      'embedded_matches_generated':report['embedded_matches_generated']}))


if __name__=='__main__':main()
