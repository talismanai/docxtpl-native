"""Direct API parity for listing expansion with preserved formatting."""
import random
import unittest
import docxtpl
import docxtpl_native


class NativeListingApiTests(unittest.TestCase):
    def test_listing_controls_and_formatting(self):
        reference = docxtpl.DocxTemplate(None)
        candidate = docxtpl_native.DocxTemplate(None)
        rng = random.Random(781)
        values = ['A', ' ', '\t', '\n', '\a', '\f', '世界', '&amp;']
        for _ in range(1000):
            text = ''.join(rng.choices(values, k=rng.randrange(12)))
            ppr = '<w:pPr><w:pStyle w:val="X"/></w:pPr>' if rng.randrange(2) else ''
            rpr = '<w:rPr><w:b/></w:rPr>' if rng.randrange(2) else ''
            xml = f'<w:p>{ppr}<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
            self.assertEqual(candidate.resolve_listing(xml), reference.resolve_listing(xml), repr(xml))
