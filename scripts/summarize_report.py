"""Keep the CI diagnosis accessible without downloading document artifacts."""
import json
import sys
from collections import Counter
from pathlib import Path

report=json.loads(Path(sys.argv[1]).read_text())
print('PARITY SUMMARY:',json.dumps(report.get('counts',{})), 'status='+report['status'])
for case in report['cases']:
    details=[]
    for role in ['reference','candidate']:
        error=case.get(role,{}).get('exception')
        if error:details.append(role+': '+error['type']+': '+error['message'].splitlines()[0])
    changes=Counter((diff['kind'],diff['part']) for doc in case.get('documents',[]) for diff in doc['differences'])
    if changes:details.append('parts='+json.dumps([list(key) for key in changes]))
    docs=case.get('documents',[])
    if isinstance(case.get('behavior'),dict):
        behavior=case['behavior'];details.append('behavior='+str(behavior['equal']))
        if not behavior['equal']:details.append(json.dumps(behavior,ensure_ascii=False))
    elif case.get('behavior'):details.append('behavior='+str(case['behavior']))
    if docs:
        details.append('content='+str(all(d['content_equal'] for d in docs)))
        details.append('format='+str(all(d['structure_and_format_equal'] for d in docs)))
        visual=[d.get('visual',{}) for d in docs]
        equal=all(v.get('equal') is True for v in visual)
        details.append('visual='+str(equal) if all(v.get('equal') is not None for v in visual)
                       else 'visual='+','.join(sorted({v.get('status','unknown') for v in visual})))
    print(case['id'],case['status'],'; '.join(details) or case.get('reason',''))
