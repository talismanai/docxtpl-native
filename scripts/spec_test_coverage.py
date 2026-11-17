"""Fail explicitly while enumerated spec contracts/partitions lack test evidence."""
import argparse
import json
from pathlib import Path
from paritylab.cases import CASES
from paritylab.protocols import PROBES
from paritylab.spec_test_coverage import assess_tests
from paritylab.standalone_evidence import CASES as STANDALONE_CASES

p = argparse.ArgumentParser()
p.add_argument('--check', action='store_true')
p.add_argument('--catalog', default='compatibility/spec_contracts.json')
p.add_argument('--upstream', default='.upstream/docxtpl/tests')
p.add_argument('--out', default='artifacts/spec-test-coverage.json')
a = p.parse_args()
known = {'synthetic/'+name for name in CASES} | {'protocol/'+name for name in PROBES}
known |= {'upstream/'+path.stem for path in Path(a.upstream).glob('*.py')}
root = Path(__file__).resolve().parents[1]
known |= {name for name, case in STANDALONE_CASES.items() if (root / case['runner']).is_file()}
result = assess_tests(json.loads(Path(a.catalog).read_text()), known)
out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(result, indent=2)+'\n')
print(json.dumps({key: len(value) if isinstance(value, list) else value for key, value in result.items()}))
if a.check and not result['complete']:
    raise SystemExit(1)
