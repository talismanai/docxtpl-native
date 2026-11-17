"""Registered output divergences from the pinned reference.

Each entry is a documented decision where the package differs from the
reference on purpose, because reproducing it would damage the document. A case
counts as a divergence, never as a pass, and only when the candidate differs
exactly as declared: the same behaviour observation (or the declared one), differences
only in the declared parts, and a part check that pins the difference. Any
other difference is still a gap.
"""
from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Divergence:
    decision: str
    catalog_id: str
    parts: frozenset[str]
    # Checks one generated document pair: (reference bytes, candidate bytes).
    check: Callable[[bytes, bytes], bool]
    # The candidate's behaviour observation; None requires the reference's.
    observation: Mapping[str, Any] | None = None
    # Whether the rendered pages may differ.
    visual: bool = False


def _part(blob: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(blob)) as package:
        return package.read(name)


def _kept_ampersand(reference: bytes, candidate: bytes) -> bool:
    # libxml2's recover mode drops the stray `&` from the reference's text.
    left = _part(reference, 'word/document.xml')
    right = _part(candidate, 'word/document.xml')
    return b'Rock &amp; Roll' in right and right.replace(b'Rock &amp; Roll', b'Rock  Roll') == left


_NSID = re.compile(rb'(<w:nsid w:val=")[0-9A-Fa-f]{8}(")')


def _derived_nsid(reference: bytes, candidate: bytes) -> bool:
    # docxcompose draws the copied list's nsid from Python's random.
    left = _part(reference, 'word/numbering.xml')
    right = _part(candidate, 'word/numbering.xml')
    return left != right and _NSID.sub(rb'\1\2', left) == _NSID.sub(rb'\1\2', right)


DIVERGENCES: dict[str, Divergence] = {
    'protocol/unescaped_body_recovery': Divergence(
        decision='docs/STATUS.md#deliberate-differences-from-docxtpl', catalog_id='docxtpl.difference.xml_recovery',
        parts=frozenset({'word/document.xml'}), check=_kept_ampersand,
        observation={'text': 'Rock & Roll'}, visual=True),
    'protocol/composer_add_numberings': Divergence(
        decision='docs/STATUS.md#deliberate-differences-from-docxtpl', catalog_id='docxcompose.difference.numbering_nsid',
        parts=frozenset({'word/numbering.xml'}), check=_derived_nsid),
}


def matches(case_id: str, reference: Mapping[str, Any], candidate: Mapping[str, Any],
            documents: list[tuple[Mapping[str, Any], bytes, bytes]]) -> bool:
    """Whether a non-equivalent case differs from the reference exactly as registered.

    `documents` holds, per generated file, the comparison and both files' bytes.
    """
    divergence = DIVERGENCES.get(case_id)
    if divergence is None or not documents:
        return False
    expected = reference.get('observation') if divergence.observation is None else dict(divergence.observation)
    if 'observation' in reference and candidate.get('observation') != expected:
        return False
    declared = False
    for comparison, left, right in documents:
        parts = {difference['part'] for difference in comparison.get('differences', [])}
        if not parts <= divergence.parts:
            return False
        if not divergence.visual and comparison.get('visual', {}).get('equal') is False:
            return False
        if parts:
            if not divergence.check(left, right):
                return False
            declared = True
    return declared
