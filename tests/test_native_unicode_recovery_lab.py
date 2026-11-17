"""Differential laboratory checks for direct lxml-owned libxml recovery.

This does not exercise document rendering or establish DOCX parity.
"""

import unittest
from importlib import import_module
from typing import Any

import docxtpl_native

etree: Any = import_module('lxml.etree')


class NativeUnicodeRecoveryLabTests(unittest.TestCase):
    def test_lxml_owned_c_parser_matches_unicode_surrogates(self) -> None:
        cases = (
            '<r><t>A\ud800B</t><u>end</u></r>',
            '<r><t>A\udfffB</t><u>end</u></r>',
            '<r><t>A&amp;\ud800B</t></r>',
            '<r a="a\ud800b"><t>end</t></r>',
            '<r><!--a\ud800b--><t>end</t></r>',
            '<r><t>A😀\ud800B</t></r>',
            '<r><t>A\ud800\udc00B</t></r>',
            '<r><t>A\udfff\udc00B</t></r>',
            '<r><t>A\udfff&amp;B</t></r>',
            '<!DOCTYPE r><r><t>A\ud800B</t></r>',
            '<!DOCTYPE r [<!ENTITY x "value">]><r><t>&x;\ud800end</t></r>',
            '<r><t>plain ASCII</t></r>',
            '<r><t>façade &amp; café</t></r>',
            '<r><t>valid 😀 text</t></r>',
        )
        for source in cases:
            with self.subTest(source=ascii(source)):
                parser = etree.XMLParser(recover=True, no_network=True)
                expected = etree.tostring(etree.fromstring(source, parser), encoding='unicode')
                actual = docxtpl_native._extension()._lab_recover_lxml_unicode(source)
                self.assertEqual(actual, expected)

    def test_lxml_owned_c_parser_matches_surrogate_positions(self) -> None:
        prefixes = ('', 'abc', '&amp;', '😀', 'a&#x41;')
        faults = ('\ud800', '\udfff', '\ud800\udc00', '\udfff\udc00')
        suffixes = ('end', '&lt;end', '😀end')
        for prefix in prefixes:
            for fault in faults:
                for suffix in suffixes:
                    source = f'<r><t>{prefix}{fault}{suffix}</t><u>end</u></r>'
                    with self.subTest(source=ascii(source)):
                        parser = etree.XMLParser(recover=True, no_network=True)
                        expected = etree.tostring(etree.fromstring(source, parser), encoding='unicode')
                        actual = docxtpl_native._extension()._lab_recover_lxml_unicode(source)
                        self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
