"""Generate API/test traceability.

--check fails unless every requirement a machine can verify is qualified by
evidence of this revision; a requirement whose evidence is manual is listed
as pending and leaves `fully_compatible` false until that evidence exists.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
from paritylab.inventory import inventory,matrix


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--contract',default='compatibility/contract.json')
    parser.add_argument('--report',default='artifacts/parity/report.json')
    parser.add_argument('--out',default='artifacts/compatibility-matrix.json')
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--deployment-report',action='append',default=[],type=Path,metavar='FILE',
        help='wheel installation report of one declared deployment target (repeatable); '
             'produced by python/paritylab/check_wheel_install.py inside that target')
    args=parser.parse_args()
    path=Path(args.report);report=json.loads(path.read_text()) if path.exists() else None
    revision=os.environ.get('GITHUB_SHA') or subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    deployment=[]
    for source in args.deployment_report:
        try:deployment.append(json.loads(source.read_text()))
        except (OSError,ValueError):deployment.append(None)  # unreadable evidence is an issue, not a crash
    result=matrix(json.loads(Path(args.contract).read_text()),inventory(),report,required_revision=revision,
                  deployment_reports=deployment)
    destination=Path(args.out);destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print('COMPATIBILITY MATRIX:',json.dumps(result['counts']),'status='+result['status'])
    for issue in result['issues']:print(issue)
    for item in result['requirements']:
        if item['manual_evidence'] and not item['qualified']:print('manual evidence pending: '+item['id'])
    return 1 if args.check and not result['automated_evidence_complete'] else 0


if __name__=='__main__':raise SystemExit(main())
