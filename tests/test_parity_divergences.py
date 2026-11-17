"""Registered parity divergences accept only the declared difference."""

from __future__ import annotations

import io
import json
import unittest
import zipfile
from pathlib import Path
from typing import Any

from paritylab import divergences
from paritylab.protocols import PROBES

DOCUMENT = (b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b'<w:body><w:p><w:r><w:t>%s</w:t></w:r></w:p></w:body></w:document>')
NUMBERING = (b'<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
             b'<w:abstractNum w:abstractNumId="9"><w:nsid w:val="%s"/><w:lvl w:ilvl="%s"/></w:abstractNum>'
             b'</w:numbering>')


def package(name: str, data: bytes) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr(name, data)
    return stream.getvalue()


def comparison(*parts: str, visual: bool | None = True) -> dict[str, Any]:
    return {'differences': [{'part': part, 'kind': 'document_content'} for part in parts], 'visual': {'equal': visual}}


class ParityDivergenceTests(unittest.TestCase):
    def test_kept_ampersand_is_the_only_accepted_difference(self) -> None:
        case = 'protocol/unescaped_body_recovery'
        reference = package('word/document.xml', DOCUMENT % b'Rock  Roll')
        kept = package('word/document.xml', DOCUMENT % b'Rock &amp; Roll')
        other = package('word/document.xml', DOCUMENT % b'Rock &amp; Roll!')
        observed = ({'observation': {'text': 'Rock  Roll'}}, {'observation': {'text': 'Rock & Roll'}})
        self.assertTrue(divergences.matches(case, *observed, [(comparison('word/document.xml', visual=False), reference, kept)]))
        self.assertFalse(divergences.matches(case, *observed, [(comparison('word/document.xml'), reference, other)]))
        self.assertFalse(divergences.matches(case, observed[0], {'observation': {'text': 'Rock Roll'}},
                                             [(comparison('word/document.xml'), reference, kept)]))
        self.assertFalse(divergences.matches(case, *observed, [(comparison('word/document.xml', 'word/styles.xml'),
                                                                 reference, kept)]))
        self.assertFalse(divergences.matches(case, *observed, [(comparison(), reference, reference)]))

    def test_derived_nsid_is_the_only_accepted_difference(self) -> None:
        case = 'protocol/composer_add_numberings'
        reference = package('word/numbering.xml', NUMBERING % (b'00BC614E', b'0'))
        derived = package('word/numbering.xml', NUMBERING % (b'D0ABDE0E', b'0'))
        changed = package('word/numbering.xml', NUMBERING % (b'D0ABDE0E', b'1'))
        same = ({'observation': {'refs': [10]}}, {'observation': {'refs': [10]}})
        self.assertTrue(divergences.matches(case, *same, [(comparison('word/numbering.xml'), reference, derived)]))
        self.assertFalse(divergences.matches(case, *same, [(comparison('word/numbering.xml'), reference, changed)]))
        self.assertFalse(divergences.matches(case, *same, [(comparison('word/numbering.xml', visual=False),
                                                             reference, derived)]))
        self.assertFalse(divergences.matches(case, same[0], {'observation': {'refs': [11]}},
                                             [(comparison('word/numbering.xml'), reference, derived)]))

    def test_unregistered_case_never_diverges(self) -> None:
        reference = package('word/document.xml', DOCUMENT % b'Rock  Roll')
        kept = package('word/document.xml', DOCUMENT % b'Rock &amp; Roll')
        self.assertFalse(divergences.matches('protocol/other', {}, {}, [(comparison('word/document.xml'), reference, kept)]))

    def test_registry_matches_the_catalog(self) -> None:
        catalog = json.loads(Path('compatibility/spec_contracts.json').read_text())
        cases = {case: entry['id'] for entry in catalog['divergences'] for case in entry.get('cases', [])}
        self.assertEqual(cases, {case: item.catalog_id for case, item in divergences.DIVERGENCES.items()})
        for case in divergences.DIVERGENCES:
            self.assertIn(case.split('/', 1)[1], PROBES)


if __name__ == '__main__':
    unittest.main()
