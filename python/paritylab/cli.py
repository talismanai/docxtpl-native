from __future__ import annotations
import argparse
import hashlib
import html
import json
import os
import platform
import statistics
import concurrent.futures
import subprocess
import threading
import sys
from pathlib import Path
from .cases import CASES,fixture
from .compare import compare_docx
from . import divergences
from . import visual
from .protocols import PROBES
from .upstream_adapters import ADAPTERS
from .upstream_manifest import MANIFEST_REFERENCE_COMMIT, UPSTREAM_EXAMPLE_SHA256, UPSTREAM_FIXTURE_SHA256
from .reference_fingerprint import REFERENCE_COMMIT

# A case whose output embeds a document it saved itself carries each side's
# wall-clock time in bytes the comparison requires to be equal. Its clock is
# fixed whatever the options, so its verdict never depends on when each side ran.
FIXED_CLOCK_CASES = frozenset({'upstream/embedded'})
FIXED_CLOCK_EPOCH = 1704067200.0


def _frozen_by_options(case_id: str, freeze_time: float | None, frozen_cases: set[str]) -> bool:
    return freeze_time is not None and (not frozen_cases or case_id in frozen_cases)


def _case_clock(case_id: str, freeze_time: float | None, frozen_cases: set[str]) -> float | None:
    """The fixed wall time of a case's workers, or None for the live clock."""
    if _frozen_by_options(case_id, freeze_time, frozen_cases):
        return freeze_time
    return FIXED_CLOCK_EPOCH if case_id in FIXED_CLOCK_CASES else None


def _upstream_examples(path: Path) -> list[Path]:
    if MANIFEST_REFERENCE_COMMIT != REFERENCE_COMMIT:
        raise ValueError('upstream example manifest revision does not match the reference')
    if not path.is_dir():
        raise ValueError('upstream missing; run scripts/fetch_upstream.py')
    scripts = {script.name: script for script in path.glob('*.py')
               if script.name not in ('__init__.py', 'runtests.py')}
    missing = sorted(set(UPSTREAM_EXAMPLE_SHA256) - scripts.keys())
    extra = sorted(scripts.keys() - set(UPSTREAM_EXAMPLE_SHA256))
    changed = sorted(name for name, script in scripts.items()
                     if name in UPSTREAM_EXAMPLE_SHA256
                     and hashlib.sha256(script.read_bytes()).hexdigest() != UPSTREAM_EXAMPLE_SHA256[name])
    if missing or extra or changed:
        raise ValueError(f'upstream example corpus differs from pinned manifest: '
                         f'missing={missing}, extra={extra}, changed={changed}')
    missing_fixtures = sorted(name for name in UPSTREAM_FIXTURE_SHA256 if not (path / name).is_file())
    changed_fixtures = sorted(name for name, digest in UPSTREAM_FIXTURE_SHA256.items()
                              if (path / name).is_file()
                              and hashlib.sha256((path / name).read_bytes()).hexdigest() != digest)
    if missing_fixtures or changed_fixtures:
        raise ValueError('upstream fixture corpus differs from pinned manifest: '
                         f'missing={missing_fixtures}, changed={changed_fixtures}')
    return [scripts[name] for name in sorted(scripts)]

def _write(path,data):
    Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2))

def _stats(worker):
    samples=worker.get('samples',[])
    if not samples:return None
    values=sorted(s['total_ms'] for s in samples)
    return {'median_total_ms':statistics.median(values),'p95_total_ms':values[min(len(values)-1,int(.95*len(values)))],
            'samples_total_ms':values,'peak_process_rss_mib':worker['peak_process_rss_mib']}

def _worker(request,out):
    try:
        if any(out.iterdir()):
            raise RuntimeError('worker output directory is not empty')
        _write(out/'request.json',request)
        command=[sys.executable,'-m','paritylab.worker',str(out/'request.json')]
        process=subprocess.run(command,capture_output=True,text=True,timeout=300)
        (out/'stdout.txt').write_text(process.stdout);(out/'stderr.txt').write_text(process.stderr)
        if process.returncode != 0 or not (out/'worker.json').is_file():
            raise RuntimeError(f'worker exit {process.returncode}; see stderr.txt')
        return json.loads((out/'worker.json').read_text())
    except Exception as e:return {'status':'worker_error','exception':{'type':type(e).__name__,'message':str(e)},'samples':[],'outputs':[]}

def source_revision():
    """The commit this report measures, and whether the tree was clean.

    `GITHUB_SHA` names it in CI. Outside CI the checkout does, so a run in a
    container or on a workstation can be qualified too — but only a clean
    tree carries a revision: with uncommitted changes the label would name a
    commit that is not what ran.
    """
    revision=os.environ.get('GITHUB_SHA')
    if revision:return {'source_revision':revision,'source_tree':'github_sha'}
    root=Path(__file__).resolve().parents[2]
    try:
        head=subprocess.run(['git','rev-parse','HEAD'],cwd=root,capture_output=True,text=True,check=True).stdout.strip()
        dirty=subprocess.run(['git','status','--porcelain'],cwd=root,capture_output=True,text=True,check=True).stdout.strip()
    except (OSError,subprocess.CalledProcessError):
        return {'source_revision':None,'source_tree':'unknown'}
    if dirty:return {'source_revision':None,'source_tree':'dirty'}
    return {'source_revision':head,'source_tree':'clean_checkout'}


def selection(args):
    """What the run was asked to measure.

    A revision qualifies on a complete corpus, never on a chosen subset: a
    selection that covers exactly the tests the contract names would qualify
    the matrix while most of the corpus never ran. The report says which it
    is, and the gates refuse an incomplete one.
    """
    chosen=sorted(getattr(args,'case',None) or []);protocols=sorted(getattr(args,'protocol_case',None) or [])
    rerun=getattr(args,'rerun_from',None);shard=getattr(args,'shard',None)
    chosen_selection={'suite':args.suite,'cases':chosen,'protocol_cases':protocols,
                      'real_manifest':bool(getattr(args,'real_manifest',None)),
                      'rerun_from':str(rerun) if rerun else None}
    # A shard is part of the corpus by construction: only the merge of all the
    # shards of one run (scripts/merge_parity_shards.py) is complete.
    return {**chosen_selection,'shard':shard,'complete':corpus_selection(chosen_selection) and not shard}


def corpus_selection(chosen: dict) -> bool:
    """Whether a selection, shards aside, asks for the whole corpus."""
    return (chosen['suite']=='all' and not chosen['cases'] and not chosen['protocol_cases']
            and not chosen['rerun_from'])


def parse_shard(value: str) -> dict[str, int]:
    """`K/N`: the K-th of N interleaved parts of the corpus, counting from 1."""
    index,_,count=value.partition('/')
    try:shard={'index':int(index),'count':int(count)}
    except ValueError:raise argparse.ArgumentTypeError('expected K/N, such as 2/6') from None
    if not 1<=shard['index']<=shard['count']:raise argparse.ArgumentTypeError('expected 1 <= K <= N')
    return shard


def run(args):
    if args.jobs<1:raise ValueError('--jobs must be at least 1')
    if args.jobs>1 and (args.repeat>1 or args.warmup>0):
        raise ValueError('--jobs>1 does not measure time: run timing comparisons with --jobs 1')
    out=Path(args.out).resolve();out.mkdir(parents=True,exist_ok=True)
    if any(out.iterdir()):
        raise ValueError('output directory is not empty; choose a fresh output directory')
    report={'schema_version':1,'mode':'self_check' if args.candidate=='reference' else 'differential',
      'candidate':'docxtpl' if args.candidate=='reference' else 'docxtpl_native',
      'reference':'docxtpl','python':platform.python_version(),'platform':platform.platform(),
      **source_revision(),
      'visual_policy':args.visual,'repeat':args.repeat,'warmup':args.warmup,'jobs':args.jobs,'cases':[],
      'selection':selection(args),
      'notes':['Generation timing excludes process startup/imports and all document comparisons.',
               'Peak process RSS includes imports and interpreter. No tracemalloc claim for Rust allocations.',
               'Speedup is reported only when document comparisons pass; timing ratios for gaps are diagnostic.',
               'Self-check compares reference with reference; it is not native parity evidence.']}
    if args.jobs>1:
        report['notes'].append('Cases ran concurrently (--jobs '+str(args.jobs)+'): comparisons are unaffected, '
                               'and no timing is reported because the host was loaded.')
    freeze_time=getattr(args,'freeze_time',None)
    frozen_cases=set(getattr(args,'freeze_time_case',None) or [])
    if frozen_cases and freeze_time is None:
        raise ValueError('--freeze-time-case requires --freeze-time')
    if freeze_time is not None:
        report['clock']={'mode':'fixed_wall_time','epoch_seconds':freeze_time}
        if frozen_cases:report['clock']['cases']=sorted(frozen_cases)
        report['notes'].append('Worker wall clocks are fixed; comparison gates remain unchanged.')
    if args.candidate=='native':
        from docxtpl_native import capabilities
        report['capabilities']=capabilities()
        if not report['capabilities']['native_available']:
            report.update(status='blocked',reason='native extension unavailable; no reference fallback')
            _finish(report,out);return 2
    cases=[]
    if args.suite in ('synthetic','all'):
        selected=args.case or list(CASES)
        for name in selected:
            if name not in CASES:raise ValueError('unknown case '+name)
            folder=out/'fixtures'/name;template=fixture(name,folder)
            cases.append({'id':'synthetic/'+name,'kind':'synthetic','case':name,'template':str(template),'fixtures':str(folder),
                          'expected_error':{'type':'jinja2.exceptions.UndefinedError',
                                            'message':"'missing' is undefined"} if name=='strict_undefined' else None})
    if args.suite in ('upstream','all'):
        path=Path(args.upstream).resolve()/'tests'
        scripts = _upstream_examples(path)
        report['upstream_examples'] = {'reference_commit': REFERENCE_COMMIT, 'count': len(scripts),
                                       'fixture_count': len(UPSTREAM_FIXTURE_SHA256)}
        for script in scripts:
            cases.append({'id':'upstream/'+script.stem,'kind':'upstream','script':script.name,'upstream_tests':str(path),
                          'requires_adapter':script.name not in ADAPTERS and ('subprocess' in script.read_text() or 'os.system' in script.read_text()),
                          'behavior_adapter':script.name in ADAPTERS,'assertion_only':script.name=='get_undeclared_variables.py'})
    if args.suite in ('protocols','all'):
        for name in getattr(args,'protocol_case',None) or list(PROBES):
            if name not in PROBES:raise ValueError('unknown protocol '+name)
            cases.append({'id':'protocol/'+name,'kind':'protocol','case':name,
                          'assertion_only':PROBES[name].assertion_only})
    if args.real_manifest:
        manifest_path=Path(args.real_manifest).resolve();manifest=json.loads(manifest_path.read_text())
        for item in manifest['cases']:
            cases.append({'id':'real/'+item['id'],'kind':'real',
                          'template':str((manifest_path.parent/item['template']).resolve()),
                          'context':str((manifest_path.parent/item['context']).resolve()),
                          'autoescape':item.get('autoescape',False)})
    if getattr(args,'rerun_from',None):
        # Development convenience: narrow the run to what did not pass before.
        # The result is a partial report by construction, and the selection it
        # records keeps it from qualifying a revision.
        previous=json.loads(Path(args.rerun_from).read_text())
        unresolved={case['id'] for case in previous.get('cases',[]) if case['status']!='pass'}
        missing=unresolved-{case['id'] for case in cases}
        if missing:raise ValueError('the earlier report has cases this suite does not build: '
                                    +', '.join(sorted(missing)))
        cases=[case for case in cases if case['id'] in unresolved]
        if not cases:raise ValueError('nothing to rerun: every case passed in '+str(args.rerun_from))
        report['notes'].append('Rerun of the cases that did not pass in an earlier report; '
                               'a partial run is not evidence for a revision.')
    unknown=frozen_cases-{case['id'] for case in cases}
    if unknown:raise ValueError('unknown frozen cases: '+', '.join(sorted(unknown)))
    own_clock=sorted(case['id'] for case in cases if case['id'] in FIXED_CLOCK_CASES
                     and not _frozen_by_options(case['id'],freeze_time,frozen_cases))
    if own_clock:report['fixed_clock_cases']={'epoch_seconds':FIXED_CLOCK_EPOCH,'cases':own_clock}
    shard=getattr(args,'shard',None)
    if shard:
        # Interleaved, so every shard gets a similar mix of slow and fast cases.
        # The corpus it was cut from lets the merge prove the shards cover it once.
        report['corpus']=[case['id'] for case in cases]
        cases=cases[shard['index']-1::shard['count']]
    def evaluate(i,case):
        folder=out/'cases'/case['id'];folder.mkdir(parents=True,exist_ok=True)
        row={'id':case['id'],'status':'gap','documents':[]}
        if case.get('requires_adapter'):
            row.update(status='unadapted',reason='example invokes child processes; candidate provenance needs an adapter')
            return row
        results={}
        for role in (('reference','candidate') if i%2==0 else ('candidate','reference')):
            working=folder/role;working.mkdir(exist_ok=True)
            request={**case,'backend':report[role],'output':str(working),'repeat':args.repeat,'warmup':args.warmup}
            clock=_case_clock(case['id'],freeze_time,frozen_cases)
            if clock is not None:
                request['freeze_time']=clock
            results[role]=_worker(request,working)
            row[role]={'status':results[role]['status'],'stage':results[role].get('stage'),
                       'exception':results[role].get('exception'),
                       'metrics':None if case['kind']=='protocol' else _stats(results[role]),'engine_version':results[role].get('engine_version'),
                       'execution_bridge':results[role].get('execution_bridge'),
                       'dependencies':results[role].get('dependencies'),
                       'reference_direct_url':results[role].get('reference_direct_url'),
                       'reference_source':results[role].get('reference_source')}
        a,b=results['reference'],results['candidate']
        if a['status']!='ok' or case.get('expected_error'):
            # Matching infrastructure failures are never counted as parity.
            expected = case.get('expected_error')
            actual = a.get('exception') or {}
            if expected and a['status']=='error' and a.get('stage')=='render' and all(actual.get(key)==value for key,value in expected.items()):
                other = b.get('exception') or {}
                row['status']='pass' if b['status']=='error' and b.get('stage')=='render' and all(other.get(key)==value for key,value in expected.items()) else 'gap'
                row['behavior']='expected_exception'
            else:row['status']='reference_error'
        elif b['status']!='ok':row['status']='gap'
        elif set(a['outputs'])!=set(b['outputs']):row.update(status='gap',reason='different output file sets')
        elif not a['outputs'] and not case.get('assertion_only'):
            row.update(status='gap',reason='no generated documents and no assertion-only contract')
        else:
            good=True
            if case['kind']=='protocol' or case.get('behavior_adapter'):
                observed='observation' in a and 'observation' in b
                same=observed and a['observation']==b['observation']
                row['behavior']={'equal':same,'reference':a.get('observation'),'candidate':b.get('observation')}
                good=good and same
            pairs=[]
            for name in a['outputs']:
                left=folder/'reference'/'documents'/name;right=folder/'candidate'/'documents'/name
                diff=compare_docx(left.read_bytes(),right.read_bytes());diff['file']=name
                pairs.append((diff,left,right))
                if args.visual!='off':
                    if visual.available():
                        try:diff['visual']=visual.compare_visual(left,right,folder/'visual'/name)
                        except Exception as e:diff['visual']={'status':'error','message':str(e),'equal':False}
                    else:diff['visual']={'status':'unavailable','equal':None}
                    if args.visual=='required' and diff['visual'].get('equal') is not True:good=False
                    if diff['visual'].get('equal') is False:good=False
                else:diff['visual']={'status':'not_requested','equal':None}
                good=good and diff['equivalent'];row['documents'].append(diff)
            row['status']='pass' if good else 'gap'
            # A documented, intentional difference never counts as a pass.
            if not good and divergences.matches(case['id'],a,b,[(d,l.read_bytes(),r.read_bytes()) for d,l,r in pairs]):
                registered=divergences.DIVERGENCES[case['id']]
                row.update(status='divergence',divergence={'catalog':registered.catalog_id,'decision':registered.decision})
            ma,mb=row['reference']['metrics'],row['candidate']['metrics']
            if ma and mb and report['mode']!='self_check' and args.jobs==1:
                ratio=ma['median_total_ms']/mb['median_total_ms']
                row['timing_ratio_reference_to_candidate']=ratio
                if good:row['speedup']=ratio
        return row

    # Cases are independent: each one runs both engines in their own
    # processes and compares the results. With --jobs they run concurrently;
    # the report keeps them in corpus order, and timings are not collected,
    # because a loaded host does not measure anything comparable.
    rows={};lock=threading.Lock()
    def finish(i,row):
        with lock:
            rows[i]=row
            print(row['id']+': '+row['status'],flush=True)
            report['cases']=[rows[key] for key in sorted(rows)]
            _write(out/'report.json',report)
    if args.jobs==1:
        for i,case in enumerate(cases):finish(i,evaluate(i,case))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            pending={pool.submit(evaluate,i,case):i for i,case in enumerate(cases)}
            for future in concurrent.futures.as_completed(pending):finish(pending[future],future.result())
    failures=sum(row['status'] not in ('pass','divergence') for row in report['cases'])
    diverged=sum(row['status']=='divergence' for row in report['cases'])
    counts={'total':len(report['cases']),'passed':len(report['cases'])-failures-diverged,'gaps_or_errors':failures}
    if diverged:counts['divergences']=diverged
    report.update(status='pass' if not failures else 'gaps',counts=counts)
    _finish(report,out)
    return 1 if failures else 0

def _finish(report,out):
    _write(out/'report.json',report)
    rows=''
    for c in report['cases']:
        a=(c.get('reference',{}).get('metrics') or {}).get('median_total_ms')
        b=(c.get('candidate',{}).get('metrics') or {}).get('median_total_ms')
        docs=c.get('documents',[])
        size=docs[0]['size_ratio'] if len(docs)==1 else None
        cells=[c['id'],c['status'],f'{a:.2f}' if a is not None else '—',
               f'{b:.2f}' if b is not None else '—',
               f"{c['speedup']:.2f}×" if 'speedup' in c else '—',
               f'{size:.3f}' if size is not None else '—',
               str(all(d['content_equal'] for d in docs)) if docs else '—',
               str(all(d['structure_and_format_equal'] for d in docs)) if docs else '—',
               ', '.join(sorted({d['visual']['status'] for d in docs})) if docs else '—']
        rows+='<tr>'+''.join('<td>'+html.escape(x)+'</td>' for x in cells)+'</tr>'
    text='<!doctype html><meta charset="utf-8"><title>DOCX parity</title><style>body{max-width:1000px;margin:40px auto;font:16px system-ui;padding:20px}td,th{padding:10px;border-bottom:1px solid #ccc;text-align:left}pre{white-space:pre-wrap}</style>'
    text+='<h1>DOCX parity · '+html.escape(report.get('status','running'))+'</h1><p>Mode: '+report['mode']+'</p>'
    text+='<p>'+html.escape(report.get('reason',''))+'</p><div style="overflow:auto"><table><tr><th>Case</th><th>Status</th><th>Reference ms</th><th>Candidate ms</th><th>Speedup</th><th>Size ratio</th><th>Content equal</th><th>XML/format equal</th><th>Visual</th></tr>'+rows+'</table></div>'
    text+='<details><summary>Full evidence</summary><pre>'+html.escape(json.dumps(report,ensure_ascii=False,indent=2))+'</pre></details>'
    (out/'report.html').write_text(text)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidate',choices=['native','reference'],default='native')
    p.add_argument('--suite',choices=['synthetic','upstream','protocols','all'],default='synthetic')
    p.add_argument('--case',action='append')
    p.add_argument('--protocol-case',action='append')
    p.add_argument('--repeat',type=int,default=7);p.add_argument('--warmup',type=int,default=1)
    p.add_argument('--visual',choices=['off','if-available','required'],default='if-available')
    p.add_argument('--upstream',default='.upstream/docxtpl')
    p.add_argument('--real-manifest');p.add_argument('--out',default='artifacts/parity')
    p.add_argument('--jobs',type=int,default=1,help='cases to evaluate concurrently; timings are collected only with 1')
    p.add_argument('--rerun-from',metavar='REPORT',
                   help='run only the cases that did not pass in this earlier report; the run is partial, '
                        'so it qualifies no revision')
    p.add_argument('--freeze-time',type=int,help='fix worker wall clock to this Unix timestamp')
    p.add_argument('--freeze-time-case',action='append',help='apply fixed clock only to this complete case ID; repeatable')
    p.add_argument('--shard',type=parse_shard,metavar='K/N',
                   help='run the K-th of N interleaved parts of the corpus; scripts/merge_parity_shards.py '
                        'joins the reports of all N into one')
    args=p.parse_args()
    if args.repeat<1 or args.warmup<0:p.error('repeat >= 1 and warmup >= 0 required')
    try:return run(args)
    except Exception as exc:print(f'parity infrastructure error: {exc}',file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())

