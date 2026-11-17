"""Render all document pairs from a frozen differential report."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from paritylab import visual


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f'path escapes corpus directory: {relative!r}')
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--report-sha256', required=True)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    report_path = args.report.resolve()
    if digest(report_path) != args.report_sha256:
        raise RuntimeError('input report digest differs from the requested report')
    report: dict[str, Any] = json.loads(report_path.read_text())
    if report.get('mode') != 'differential' or report.get('candidate') != 'docxtpl_native':
        raise RuntimeError('report must compare the native candidate with the reference')
    capabilities = report.get('capabilities') or {}
    if not capabilities.get('native_available') or capabilities.get('reference_fallback'):
        raise RuntimeError('native engine availability or fallback status is unverified')
    if not visual.available():
        raise RuntimeError('required visual renderer is unavailable')
    if visual.__file__ is None:
        raise RuntimeError('visual comparator source path is unavailable')
    visual_path = Path(visual.__file__).resolve()
    visual_sha256 = digest(visual_path)

    pairs: list[tuple[str, dict[str, Any], Path, Path]] = []
    corpus = report_path.parent
    case_ids: set[str] = set()
    for case in report['cases']:
        if case['id'] in case_ids:
            raise RuntimeError(f"duplicate input case: {case['id']}")
        case_ids.add(case['id'])
        if case['status'] != 'pass':
            raise RuntimeError(f"input case is not a pass: {case['id']}")
        if any(not (case[role].get('reference_source') or {}).get('matched')
               for role in ('reference', 'candidate')):
            raise RuntimeError(f"reference fingerprint missing or mismatched: {case['id']}")
        case_root = inside(corpus / 'cases', case['id'])
        names: set[str] = set()
        for doc in case['documents']:
            if doc['file'] in names:
                raise RuntimeError(f"duplicate input document: {case['id']}/{doc['file']}")
            names.add(doc['file'])
            if not doc['equivalent']:
                raise RuntimeError(f"input document is not structurally equivalent: {case['id']}")
            reference = inside(case_root / 'reference' / 'documents', doc['file'])
            candidate = inside(case_root / 'candidate' / 'documents', doc['file'])
            pairs.append((case['id'], doc, reference, candidate))
    if not pairs:
        raise RuntimeError('report has no documents to compare')

    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=False)
    records: list[dict[str, Any]] = []
    with (output / 'results.jsonl').open('x', encoding='utf-8') as stream:
        for index, (case_id, doc, reference, candidate) in enumerate(pairs, 1):
            if digest(reference) != doc['reference_sha256'] or digest(candidate) != doc['candidate_sha256']:
                raise RuntimeError(f'input document drift before rendering: {case_id}/{doc["file"]}')
            try:
                result = visual.compare_visual(reference, candidate, output / 'rendered' / f'{index:05d}')
            except Exception as error:  # noqa: BLE001 - failures are evidence, not visual passes
                result = {'status': 'error', 'equal': False,
                          'type': type(error).__name__, 'message': str(error)}
            if digest(reference) != doc['reference_sha256'] or digest(candidate) != doc['candidate_sha256']:
                raise RuntimeError(f'input document drift during rendering: {case_id}/{doc["file"]}')
            record = {'case': case_id, 'file': doc['file'], 'reference_sha256': doc['reference_sha256'],
                      'candidate_sha256': doc['candidate_sha256'], 'visual': result}
            records.append(record)
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
            print(f'{index}/{len(pairs)} {case_id}/{doc["file"]}: {result["status"]} equal={result["equal"]}',
                  flush=True)

    if digest(report_path) != args.report_sha256:
        raise RuntimeError('input report drift during rendering')
    if digest(visual_path) != visual_sha256:
        raise RuntimeError('visual comparator source drift during rendering')
    summary = {'report': str(report_path), 'report_sha256': args.report_sha256,
               'visual_module': str(visual_path), 'visual_module_sha256': visual_sha256, 'pairs': len(pairs),
               'equal': sum(record['visual']['status'] == 'compared' and record['visual']['equal'] is True
                            for record in records),
               'different': sum(record['visual']['status'] == 'compared' and record['visual']['equal'] is False
                                for record in records),
               'errors': sum(record['visual']['status'] != 'compared' for record in records)}
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if summary['equal'] != len(pairs) or summary['different'] or summary['errors']:
        raise SystemExit('visual corpus contains differences or errors')


if __name__ == '__main__':
    main()
