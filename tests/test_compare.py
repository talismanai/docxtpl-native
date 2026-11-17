import io
import unittest
import zipfile
from docx import Document
from lxml import etree
from paritylab.compare import compare_docx,inspect_docx,CT

def document(text='Hello'):
    d=Document();d.add_paragraph(text);b=io.BytesIO();d.save(b);return b.getvalue()

def mutate(blob,name,fn):
    out=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(blob)) as src,zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data=src.read(item.filename)
            if item.filename==name:data=fn(data)
            dst.writestr(item,data)
    return out.getvalue()

def embed_document(outer,inner):
    out=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(outer)) as src,zipfile.ZipFile(out,'w') as dst:
        for item in src.infolist():
            data=src.read(item.filename)
            if item.filename=='[Content_Types].xml':
                root=etree.fromstring(data)
                etree.SubElement(root,'{'+CT+'}Override',PartName='/word/embeddings/nested.docx',
                                 ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
                data=etree.tostring(root)
            dst.writestr(item,data)
        dst.writestr('word/embeddings/nested.docx',inner)
    return out.getvalue()

def change_zip_timestamps(blob):
    out=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(blob)) as src,zipfile.ZipFile(out,'w') as dst:
        for item in src.infolist():
            data=src.read(item.filename);item.date_time=(2001,1,1,0,0,0)
            dst.writestr(item,data)
    return out.getvalue()

def corrupt_inner_crc():
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as archive:
        archive.writestr('[Content_Types].xml','<Types/>')
        archive.writestr('_rels/.rels','<Relationships/>')
        archive.writestr('word/document.xml','<doc>Marker</doc>')
    return out.getvalue().replace(b'Marker',b'BROKEN',1)

class ComparisonTests(unittest.TestCase):
    def test_identical_payloads_pass(self):
        a=document();self.assertTrue(compare_docx(a,a)['equivalent'])
    def test_text_change_fails(self):
        a=document('A');b=document('B');r=compare_docx(a,b)
        self.assertFalse(r['equivalent']);self.assertFalse(r['content_equal'])
    def test_same_text_different_font_fails_format(self):
        a=document()
        def bold(data):return data.replace(b'<w:r>',b'<w:r><w:rPr><w:b/></w:rPr>',1)
        r=compare_docx(a,mutate(a,'word/document.xml',bold))
        self.assertTrue(r['content_equal']);self.assertFalse(r['structure_and_format_equal'])
    def test_style_part_changes_are_compared(self):
        def change_style(data):
            root=etree.fromstring(data)
            style=root.find('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}style')
            style.set('changed-for-test','true')
            return etree.tostring(root)
        a=document();b=mutate(a,'word/styles.xml',change_style)
        self.assertFalse(compare_docx(a,b)['equivalent'])
    def test_comment_removed_fails(self):
        a=document();b=mutate(a,'word/document.xml',lambda x:x.replace(b'</w:body>',b'<!--keep--></w:body>'))
        self.assertFalse(compare_docx(a,b)['structure_and_format_equal'])
    def test_processing_instruction_removed_fails(self):
        a=document();b=mutate(a,'word/document.xml',lambda x:x.replace(b'</w:body>',b'<?custom value?></w:body>'))
        self.assertFalse(compare_docx(a,b)['equivalent'])
    def test_attribute_order_is_not_a_difference(self):
        a=document()
        def reorder(x):
            root=etree.fromstring(x)
            for element in root.iter():
                attrs=list(element.attrib.items());element.attrib.clear();element.attrib.update(reversed(attrs))
            declaration=x[:x.index(b'?>')+2] if x.startswith(b'<?xml') else b''
            return declaration+etree.tostring(root)
        self.assertTrue(compare_docx(a,mutate(a,'word/document.xml',reorder))['equivalent'])

    def test_ignorable_namespace_binding_changes_are_detected(self):
        a=document()
        uri=b'http://schemas.microsoft.com/office/word/2010/wordml'
        with zipfile.ZipFile(io.BytesIO(a)) as package:
            self.assertIn(b'mc:Ignorable="w14 wp14"',package.read('word/document.xml'))
        b=mutate(a,'word/document.xml',lambda xml:xml.replace(uri,b'urn:changed-w14-binding'))
        result=compare_docx(a,b)
        self.assertTrue(result['content_equal'])
        self.assertFalse(result['structure_and_format_equal'])

    def test_xsi_type_namespace_binding_changes_are_detected(self):
        def add_type(xml):
            root=etree.fromstring(xml)
            replacement=etree.Element(root.tag,nsmap={**root.nsmap,'dtype':'urn:original-type'})
            replacement.attrib.update(root.attrib)
            replacement.set('{http://www.w3.org/2001/XMLSchema-instance}type','dtype:DocumentType')
            replacement.extend(root)
            return etree.tostring(replacement)
        a=mutate(document(),'word/document.xml',add_type)
        b=mutate(a,'word/document.xml',lambda xml:xml.replace(b'urn:original-type',b'urn:changed-type'))
        result=compare_docx(a,b)
        self.assertTrue(result['content_equal'])
        self.assertFalse(result['structure_and_format_equal'])
    def test_malformed_xml_is_not_recovered(self):
        a=document();b=mutate(a,'word/document.xml',lambda _:b'<broken>')
        self.assertFalse(compare_docx(a,b)['equivalent']);self.assertTrue(inspect_docx(b)['errors'])
    def test_dangling_relationship_fails(self):
        a=document()
        def dangling(x):return x.replace(b'Target="styles.xml"',b'Target="missing.xml"')
        self.assertTrue(inspect_docx(mutate(a,'word/_rels/document.xml.rels',dangling))['errors'])
    def test_binary_part_changes_are_detected(self):
        a=document();b=mutate(a,'docProps/thumbnail.jpeg',lambda x:x+b'changed')
        self.assertFalse(compare_docx(a,b)['content_equal'])

    def test_embedded_docx_diagnostic_keeps_strict_binary_failure(self):
        inner=document('same');outer=document('outer')
        left=embed_document(outer,inner)
        right=embed_document(outer,change_zip_timestamps(inner))
        result=compare_docx(left,right)
        self.assertFalse(result['equivalent'])
        difference=next(x for x in result['differences'] if x['part']=='word/embeddings/nested.docx')
        self.assertEqual(difference['kind'],'binary_content')
        self.assertTrue(difference['embedded_docx']['equivalent'])

    def test_embedded_docx_content_change_and_invalid_zip_remain_visible(self):
        outer=document('outer');left=embed_document(outer,document('before'))
        for inner in (document('after'),b'not a zip'):
            with self.subTest(inner=inner[:12]):
                result=compare_docx(left,embed_document(outer,inner))
                self.assertFalse(result['equivalent'])
                nested=next(x['embedded_docx'] for x in result['differences'] if x['part']=='word/embeddings/nested.docx')
                self.assertFalse(nested['equivalent'])

    def test_embedded_crc_failure_is_diagnostic_not_a_runner_crash(self):
        outer=document('outer')
        result=compare_docx(embed_document(outer,document('valid')),embed_document(outer,corrupt_inner_crc()))
        self.assertFalse(result['equivalent'])
        nested=next(x['embedded_docx'] for x in result['differences'] if x['part']=='word/embeddings/nested.docx')
        self.assertFalse(nested['equivalent'])
        self.assertTrue(any('word/document.xml' in error and 'BadZipFile' in error
                            for error in nested['candidate_errors']))

    def test_size_is_recorded_separately(self):
        a=document();r=compare_docx(a,a)
        self.assertEqual(r['size_ratio'],1);self.assertEqual(r['reference_size_bytes'],len(a))
    def test_zip_corruption_fails(self):self.assertTrue(inspect_docx(b'not a zip')['errors'])
    def test_missing_parts_fail(self):
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:z.writestr('word/document.xml','<x/>')
        self.assertTrue(inspect_docx(b.getvalue())['errors'])

if __name__=='__main__':unittest.main()

