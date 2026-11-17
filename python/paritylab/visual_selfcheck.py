"""Check exact-pixel stability when rendering the same DOCX repeatedly."""
import argparse
import hashlib
import json
import platform
import subprocess
import tempfile
from pathlib import Path

from . import visual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--document', required=True)
    parser.add_argument('--candidate', help='Optional second fixed input for repeated differential rendering')
    parser.add_argument('--pairs', type=int, default=5)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    if args.pairs <= 0: parser.error('pairs must be positive')
    if not visual.available(): raise RuntimeError('visual renderer unavailable')
    document = Path(args.document).resolve()
    candidate = Path(args.candidate).resolve() if args.candidate else document
    digest = hashlib.sha256(document.read_bytes()).hexdigest()
    candidate_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    reports = []
    with tempfile.TemporaryDirectory(prefix='visual-selfcheck-') as folder:
        for index in range(args.pairs):
            if hashlib.sha256(document.read_bytes()).hexdigest() != digest or hashlib.sha256(candidate.read_bytes()).hexdigest() != candidate_digest:
                raise RuntimeError('input document changed during the self-check')
            reports.append(visual.compare_visual(document, candidate, Path(folder)/str(index)))
    version = subprocess.run(['soffice', '--version'], capture_output=True, text=True, check=True).stdout.strip()
    report = {'scope': 'Two fixed input documents rendered independently in every pair.' if args.candidate
                       else 'Same exact input document rendered independently in both positions of each pair.',
              'document_sha256': digest, 'candidate_sha256': candidate_digest, 'python': platform.python_version(), 'renderer_version': version,
              'pairs': args.pairs, 'equal': sum(result['equal'] for result in reports), 'comparisons': reports}
    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({'pairs': args.pairs, 'equal': report['equal'], 'document_sha256': digest}))


if __name__ == '__main__': main()
