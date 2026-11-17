"""Differential tests of the exposed table repair API."""
import unittest
import random
from lxml import etree
import docxtpl
import docxtpl_native

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
def doc(body, prefix='w'):
    return f'<{prefix}:document xmlns:{prefix}="{W}">{body}</{prefix}:document>'
def table(widths, rows):
    cols = ''.join('<w:gridCol' + (f' w:w="{w}"' if w is not None else '') + '/>' for w in widths)
    return '<w:tbl><w:tblGrid>' + cols + '</w:tblGrid>' + rows + '</w:tbl>'
def row(*spans):
    return '<w:tr>' + ''.join('<w:tc>' + (f'<w:tcPr><w:gridSpan w:val="{span}"/></w:tcPr>' if span is not None else '') + '</w:tc>' for span in spans) + '</w:tr>'
def observe(engine, xml):
    try:
        result = engine.DocxTemplate(None).fix_tables(xml)
        return ('ok', type(result).__name__, etree.tostring(result))
    except Exception as error:
        return ('error', type(error).__name__, str(error))

class NativeFixTablesTests(unittest.TestCase):
    def compare(self, xml):
        self.assertEqual(observe(docxtpl_native, xml), observe(docxtpl, xml), repr(xml))

    def test_add_remove_and_spans(self):
        scenarios = [table(['100','100'], row(None,None,None)), table(['100','200','300'], row(None,None)), table(['100','200','300'], row(2,None)), table(['99','101'], row(None,None,None,None)), table(['0'], row(None,None)), table([], row(None)), table(['100'], ''), table(['100','200'], row(None) + row(None,None,None))]
        for scenario in scenarios:
            with self.subTest(scenario=scenario): self.compare(doc(scenario))

    def test_namespace_recovery_and_types(self):
        for xml in [doc(''), doc(table(['100'], row(None))).encode(), doc(table(['100'], row(None)))[:-13], b'<root/>', b'', None, 42, '<x:root xmlns:x="urn:x"/>', '<w:root xmlns:w="urn:x"><w:tbl/></w:root>', '<?xml version="1.0" encoding="UTF-8"?><w:root xmlns:w="urn:x"/>']:
            with self.subTest(xml=xml): self.compare(xml)

    def test_invalid_widths_and_spans(self):
        for xml in [doc(table(['bad'], row(None,None))), doc(table([None], row(None,None))), doc(table(['10',None], row(None,None,None))), doc(table(['10','bad'], row(None))), doc(table(['NaN','10'], row(None))), doc(table(['inf','10'], row(None))), doc(table(['10'], row('bad'))), doc(table(['10'], row('99999999999999999999999999999999999999999999999999999999999999999999'))), doc(table(['10'], row('-2')))]:
            with self.subTest(xml=xml): self.compare(xml)

    def test_nested_tables_and_namespace_prefix(self):
        nested = table(['80','40'], '<w:tr><w:tc>' + table(['30'], row(None,None)) + '</w:tc></w:tr>')
        self.compare(doc(nested))
        self.compare('<w:root xmlns:w="urn:other"><w:tbl><w:tblGrid><w:gridCol w:w="100"/></w:tblGrid><w:tr><w:tc/><w:tc/></w:tr></w:tbl></w:root>')

    def test_generated_grids(self):
        rng = random.Random(7919)
        widths = [None, '0', '1', '101.5', '-20', 'NaN', 'inf', 'bad']
        spans = [None, '0', '1', '2', '-1', 'bad', '99999999999999999999999999999999']
        for case in range(512):
            cols = [rng.choice(widths) for _ in range(rng.randrange(6))]
            rows = ''.join(row(*(rng.choice(spans) for _ in range(rng.randrange(6)))) for _ in range(rng.randrange(5)))
            with self.subTest(case=case): self.compare(doc(table(cols, rows)))

if __name__ == '__main__': unittest.main()
