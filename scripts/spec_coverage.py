"""Report granular contracts without promoting passing examples into coverage."""
import argparse
import json
import subprocess
from pathlib import Path
from paritylab.spec_coverage import assess,markdown
from paritylab.reference_fingerprint import REFERENCE_COMMIT

p=argparse.ArgumentParser()
p.add_argument('--catalog',default='compatibility/spec_contracts.json')
p.add_argument('--report',default='artifacts/parity/report.json')
p.add_argument('--revision',help='Exact engine revision associated with the evidence; defaults to HEAD')
p.add_argument('--out',default='artifacts/spec-coverage.json')
p.add_argument('--markdown-out',default='artifacts/spec-coverage.md')
p.add_argument('--reference-contract',default='compatibility/contract.json')
p.add_argument('--standalone-report',action='append',default=[],help='local wheel smoke evidence; may be repeated')
a=p.parse_args()
revision=a.revision or subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
contract=json.loads(Path(a.reference_contract).read_text())
if contract['reference_commit'] != REFERENCE_COMMIT:raise ValueError('reference fingerprint does not match contract commit')
path=Path(a.report);result=assess(json.loads(Path(a.catalog).read_text()),json.loads(path.read_text()) if path.exists() else None,revision,expected_versions=contract['versions'],
    standalone_reports=[json.loads(Path(path).read_text()) for path in a.standalone_report])
out=Path(a.out);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,indent=2)+'\n')
md=Path(a.markdown_out);md.parent.mkdir(parents=True,exist_ok=True);md.write_text(markdown(result))
print(json.dumps({k:result[k] for k in ('evidence_valid','standalone_case_statuses','revision','counts','qualified')}))
