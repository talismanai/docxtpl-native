"""Independently audit saved page pixels and records from a visual corpus run."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from PIL import Image


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise RuntimeError(f'path escapes corpus directory: {relative!r}')
    return path


def audit_pages(reference: Path, candidate: Path, expected: int) -> list[dict[str, Any]]:
    left = sorted(reference.glob('page-*.png'))
    right = sorted(candidate.glob('page-*.png'))
    if expected < 1 or len(left) != expected or [p.name for p in left] != [p.name for p in right]:
        raise RuntimeError('missing or unexpected rendered pages')
    pages: list[dict[str, Any]] = []
    for a, b in zip(left, right, strict=True):
        first_sha256, second_sha256 = digest(a), digest(b)
        with Image.open(a) as first, Image.open(b) as second:
            first.load()
            second.load()
            if first.mode != second.mode or first.size != second.size or first.tobytes() != second.tobytes():
                raise RuntimeError(f'rendered pixels differ: {a.name}')
            pages.append({'file': a.name, 'width': first.width, 'height': first.height,
                          'mode': first.mode, 'pixels_sha256': hashlib.sha256(first.tobytes()).hexdigest(),
                          'reference_png_sha256': first_sha256, 'candidate_png_sha256': second_sha256})
        if digest(a) != first_sha256 or digest(b) != second_sha256:
            raise RuntimeError(f'rendered page drift during audit: {a.name}')
    return pages


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--report-sha256', required=True)
    parser.add_argument('--visual-sha256', required=True)
    parser.add_argument('--results', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    report_path = args.report.resolve()
    if digest(report_path) != args.report_sha256:
        raise RuntimeError('input report digest mismatch')
    report: dict[str, Any] = json.loads(report_path.read_text())
    if report.get('mode') != 'differential' or report.get('candidate') != 'docxtpl_native':
        raise RuntimeError('report does not identify the native differential run')
    capabilities = report.get('capabilities') or {}
    if not capabilities.get('native_available') or capabilities.get('reference_fallback'):
        raise RuntimeError('native engine availability or fallback status is unverified')
    names: set[str] = set()
    for case in report['cases']:
        if case['id'] in names or case['status'] != 'pass':
            raise RuntimeError(f"invalid or duplicate case: {case['id']}")
        names.add(case['id'])
        if any(not (case[role].get('reference_source') or {}).get('matched')
               for role in ('reference', 'candidate')):
            raise RuntimeError(f"reference fingerprint missing or mismatched: {case['id']}")
        if any(not doc['equivalent'] for doc in case['documents']):
            raise RuntimeError(f"document is not structurally equivalent: {case['id']}")
        document_names = [doc['file'] for doc in case['documents']]
        if len(document_names) != len(set(document_names)):
            raise RuntimeError(f"duplicate document in case: {case['id']}")
    expected = [(case['id'], doc) for case in report['cases'] for doc in case['documents']]
    if not expected:
        raise RuntimeError('input report has no documents')
    output = args.results.resolve()
    summary_path, records_path = output / 'summary.json', output / 'results.jsonl'
    summary_sha256, records_sha256 = digest(summary_path), digest(records_path)
    summary: dict[str, Any] = json.loads(summary_path.read_text())
    records: list[dict[str, Any]] = [json.loads(line) for line in records_path.read_text().splitlines()]
    if (Path(summary['report']).resolve() != report_path or summary['report_sha256'] != args.report_sha256
            or summary['visual_module_sha256'] != args.visual_sha256):
        raise RuntimeError('visual run provenance mismatch')
    visual_module = Path(summary['visual_module'])
    if digest(visual_module) != args.visual_sha256:
        raise RuntimeError('visual comparator source has drifted')
    if len(records) != len(expected) or summary['pairs'] != len(expected):
        raise RuntimeError('visual results are incomplete or contain extra pairs')

    checked: list[dict[str, Any]] = []
    for index, ((case_id, doc), record) in enumerate(zip(expected, records, strict=True), 1):
        if (record['case'], record['file'], record['reference_sha256'], record['candidate_sha256']) != (
            case_id, doc['file'], doc['reference_sha256'], doc['candidate_sha256']
        ):
            raise RuntimeError(f'visual record mismatch at {index}')
        case_root = inside(report_path.parent / 'cases', case_id)
        reference = inside(case_root / 'reference' / 'documents', doc['file'])
        candidate = inside(case_root / 'candidate' / 'documents', doc['file'])
        if digest(reference) != doc['reference_sha256'] or digest(candidate) != doc['candidate_sha256']:
            raise RuntimeError(f'document input has drifted at {index}')
        visual = record['visual']
        count = visual.get('reference_pages', 0)
        if (visual.get('status') != 'compared' or visual.get('equal') is not True or count < 1
                or visual.get('candidate_pages') != count or visual.get('differences') != []):
            raise RuntimeError(f'visual comparison failed or has no pages at {index}')
        folder = output / 'rendered' / f'{index:05d}'
        pages = audit_pages(folder / 'reference', folder / 'candidate', count)
        if digest(reference) != doc['reference_sha256'] or digest(candidate) != doc['candidate_sha256']:
            raise RuntimeError(f'document input drift during audit at {index}')
        checked.append({'case': case_id, 'file': doc['file'], 'pages': pages})
    if (digest(report_path) != args.report_sha256 or digest(visual_module) != args.visual_sha256
            or digest(summary_path) != summary_sha256 or digest(records_path) != records_sha256):
        raise RuntimeError('report, results or comparator source drifted during audit')
    result = {'report_sha256': args.report_sha256, 'visual_module_sha256': args.visual_sha256,
              'summary_sha256': summary_sha256, 'records_sha256': records_sha256,
              'pairs': len(checked), 'pages': sum(len(row['pages']) for row in checked), 'records': checked}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'pairs': result['pairs'], 'pages': result['pages']}))


if __name__ == '__main__':
    main()
