"""Generated DOCX archives must match every byte with a fixed clock."""
import io
import random
import unittest
from unittest.mock import patch
import zipfile

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.part import Part
from docx.opc.packuri import PackURI
import docxtpl
import docxtpl_native


class NativeZipBytesTests(unittest.TestCase):
    def test_full_zip_bytes_for_generated_and_live_document_paths(self):
        source = io.BytesIO()
        document = Document()
        document.add_paragraph('Café {{ name }}')
        document.sections[0].header.paragraphs[0].text = 'Header'
        # Opaque/media-like parts cannot reuse an arbitrary input deflate stream.
        payload = random.Random(42).randbytes(128 * 1024)
        part = Part(PackURI('/word/media/opaque.png'), 'image/png', payload, document.part.package)
        document.part.relate_to(part, RT.IMAGE)
        document.save(source)
        stored = io.BytesIO()
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(stored, 'w') as archive:
            for entry in original.infolist():
                content = original.read(entry)
                entry.compress_type = zipfile.ZIP_STORED
                archive.writestr(entry, content)

        def save(engine, mode):
            template = engine.DocxTemplate(io.BytesIO(stored.getvalue()))
            if mode == 'live':
                template.get_docx()
            if mode != 'save':
                template.render({'name': 'Ada'})
            output = io.BytesIO()
            with patch('zipfile.time.time', return_value=1700000000):
                template.save(output)
            return output.getvalue()

        for mode in ['render', 'live', 'save']:
            with self.subTest(mode=mode):
                self.assertEqual(save(docxtpl_native, mode), save(docxtpl, mode))
