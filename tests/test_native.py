import io
import unittest
from docx import Document
from docxtpl_native import DocxTemplate,capabilities,compose

@unittest.skipUnless(capabilities()['native_available'],'native extension not built; this is not a parity pass')
class NativeTests(unittest.TestCase):
    def blob(self,text):
        d=Document();d.add_paragraph(text);b=io.BytesIO();d.save(b);return b.getvalue()
    def test_native_generation_is_real(self):
        t=DocxTemplate(io.BytesIO(self.blob('Hello {{ name }}')));t.render({'name':'Rust'})
        b=io.BytesIO();t.save(b);b.seek(0)
        self.assertEqual(Document(b).paragraphs[0].text,'Hello Rust')
    def test_native_composition(self):
        result=compose([self.blob('Part A'),self.blob('Part B')])
        text=''.join(p.text for p in Document(io.BytesIO(result)).paragraphs)
        self.assertIn('Part A',text);self.assertIn('Part B',text)
    def test_libxml_rejects_malformed_xml(self):
        from docxtpl_native import _native
        with self.assertRaises(ValueError):_native.validate_xml(b'<unclosed>')
    def test_complete_plain_document_matches_reference(self):
        from docxtpl import DocxTemplate as Reference
        from paritylab.compare import compare_docx
        template=self.blob('Hello {{ name }}')
        outputs=[]
        for engine in [Reference,DocxTemplate]:
            doc=engine(io.BytesIO(template));doc.render({'name':'Rust'})
            buffer=io.BytesIO();doc.save(buffer);outputs.append(buffer.getvalue())
        diff=compare_docx(*outputs)
        self.assertTrue(diff['equivalent'],diff)


    def test_native_source_buffer_types_match(self):
        from docxtpl_native import _native
        import zipfile
        template=self.blob('Buffers')
        documents=[]
        for source in (template,bytearray(template),memoryview(template),list(template)):
            data=_native.process_docx(source,'{}',False,False)
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                documents.append({name:z.read(name) for name in z.namelist()})
        self.assertTrue(all(doc==documents[0] for doc in documents[1:]))
