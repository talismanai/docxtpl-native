"""Private worker: run one backend in one fresh interpreter."""
import importlib
import importlib.metadata
import io
import json
import platform
import resource
import runpy
import shutil
import sys
import time
import traceback
from pathlib import Path
from .cases import inputs
from .protocols import execute as execute_protocol
from .upstream_adapters import ADAPTERS,execute as execute_adapted
from .reference_fingerprint import installed_reference

def main():
    request=json.loads(Path(sys.argv[1]).read_text());out=Path(request['output']);out.mkdir(parents=True,exist_ok=True)
    if 'freeze_time' in request:
        fixed=float(request['freeze_time'])
        time.time=lambda:fixed
    report={'backend':request['backend'],'python':platform.python_version(),'samples':[], 'stage':'setup',
            'status':'error','outputs':[],'exception':None}
    working=None
    try:
        lib=importlib.import_module(request['backend'])
        if request['backend']=='docxtpl_native':
            report['capabilities']=lib.capabilities()
            if not report['capabilities']['native_available']:raise RuntimeError('native extension unavailable; reference fallback forbidden')
        report['reference_source']=installed_reference()
        report['engine_version']=getattr(lib,'__version__',None)
        report['engine_module']=str(getattr(lib,'__file__','unknown'))
        report['dependencies']={}
        for package in ['docxtpl','Jinja2','python-docx','lxml','docxcompose','setuptools','Babel','Pillow','PyMuPDF']:
            try:report['dependencies'][package]=importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:report['dependencies'][package]='source tree / not installed as distribution'
        try:report['reference_direct_url']=json.loads(importlib.metadata.distribution('docxtpl').read_text('direct_url.json') or 'null')
        except (importlib.metadata.PackageNotFoundError,TypeError):report['reference_direct_url']=None
        for index in range(request['warmup']+request['repeat']):
            report['stage']='setup'
            if request['kind']=='upstream':
                working=out/'work'
                shutil.copytree(request['upstream_tests'],working)
                # An output committed in the source corpus cannot be evidence
                # that this backend generated a document.
                shutil.rmtree(working/'output',ignore_errors=True)
                (working/'output').mkdir()
            t0=time.perf_counter_ns();cpu0=time.process_time_ns()
            if request['kind']=='synthetic':
                tpl=lib.DocxTemplate(request['template'])
                context,env,autoescape=inputs(request['case'],lib,tpl,request['fixtures'])
                report['stage']='render'
                tr=time.perf_counter_ns();tpl.render(context,jinja_env=env,autoescape=autoescape)
                report['stage']='save'
                ts=time.perf_counter_ns();buffer=io.BytesIO();tpl.save(buffer)
                end=time.perf_counter_ns();cpu_end=time.process_time_ns();outputs={'document.docx':buffer.getvalue()}
                render_ms=(ts-tr)/1e6;save_ms=(end-ts)/1e6
            elif request['kind']=='real':
                tpl=lib.DocxTemplate(request['template']);context=json.loads(Path(request['context']).read_text())
                report['stage']='render'
                tr=time.perf_counter_ns();tpl.render(context,autoescape=request.get('autoescape',False))
                report['stage']='save'
                ts=time.perf_counter_ns();buffer=io.BytesIO();tpl.save(buffer)
                end=time.perf_counter_ns();cpu_end=time.process_time_ns();outputs={'document.docx':buffer.getvalue()}
                render_ms=(ts-tr)/1e6;save_ms=(end-ts)/1e6
            elif request['kind']=='protocol':
                report['stage']='protocol'
                result=execute_protocol(request['case'],lib,validate_reference=request['backend']=='docxtpl')
                end=time.perf_counter_ns();cpu_end=time.process_time_ns()
                outputs=result.outputs;render_ms=save_ms=None
                if 'observation' in report and report['observation']!=result.observation:
                    raise AssertionError('protocol observation changed between samples')
                report['observation']=result.observation
                report['timing_scope']='protocol setup, generation and assertions; not a generation benchmark'
            else:
                report['stage']='upstream'
                # Each upstream example gets a complete private copy. Its
                # imports resolve to the chosen backend, including subprocesses
                # only when the example itself uses this interpreter.
                import os
                previous=os.getcwd();saved=sys.modules.get('docxtpl');sys.modules['docxtpl']=lib
                try:
                    os.chdir(working)
                    if request['script'] in ADAPTERS:
                        report['observation']=execute_adapted(working/request['script'],request['backend'],out)
                    else:runpy.run_path(str(working/request['script']),run_name='__main__')
                finally:
                    os.chdir(previous)
                    if saved is not None:sys.modules['docxtpl']=saved
                end=time.perf_counter_ns();cpu_end=time.process_time_ns();render_ms=save_ms=None
                outputs={str(p.relative_to(working/'output')):p.read_bytes() for p in (working/'output').rglob('*.docx')}
                if not outputs and request['script']!='get_undeclared_variables.py':
                    raise RuntimeError('upstream script produced no DOCX; requires explicit assertion adapter')
            if request['backend']=='docxtpl_native' and request['kind'] in ('synthetic','real'):
                report['last_execution']=tpl.last_execution
            sample={'total_ms':(end-t0)/1e6,'cpu_ms':(cpu_end-cpu0)/1e6,
                    'render_ms':render_ms,'save_ms':save_ms}
            if working is not None:
                shutil.rmtree(working);working=None
            if index>=request['warmup']:report['samples'].append(sample)
            if index==request['warmup']+request['repeat']-1:
                for name,data in outputs.items():
                    destination=out/'documents'/name;destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(data)
                    report['outputs'].append(name)
        report['status']='ok'
        if request['backend']=='docxtpl_native':
            report['execution_bridge']='CPython object references; native MiniJinja template executor'
    except BaseException as exc:
        report['exception']={'type':type(exc).__module__+'.'+type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()}
    finally:
        if working is not None:shutil.rmtree(working,ignore_errors=True)
    # Linux reports KiB; macOS reports bytes. This includes interpreter/imports,
    # not only generation. It is a process high-water RSS, not tracemalloc.
    rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report['peak_process_rss_mib']=rss/(1024**2 if sys.platform=='darwin' else 1024)
    (out/'worker.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()

