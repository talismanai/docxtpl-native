"""Rendered bodies adopt namespaces as lxml does when map_tree replaces them."""
import io
import unittest
import zipfile
from types import ModuleType

from docx import Document
import docxtpl
import docxtpl_native
import jinja2

W14 = 'http://schemas.microsoft.com/office/word/2010/wordml'
WP14 = 'http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing'


def template() -> bytes:
    """A paragraph rebinding `wp14` to the w14 URI, as in upstream's vMerge fixture."""
    doc = Document()
    doc.add_paragraph('tail')
    source = io.BytesIO()
    doc.save(source)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source.getvalue())) as old, zipfile.ZipFile(output, 'w') as new:
        for item in old.infolist():
            data = old.read(item)
            if item.filename == 'word/document.xml':
                text = data.decode()
                assert f'xmlns:w14="{W14}"' in text and f'xmlns:wp14="{WP14}"' in text
                text = text.replace('<w:body>', '<w:body><w:p w14:paraId="00E435F6" wp14:textId="77777777" '
                    f'xmlns:wp14="{W14}"><w:r><w:t>{{{{ value }}}}</w:t></w:r></w:p>', 1)
                data = text.encode()
            new.writestr(item, data)
    return output.getvalue()


def document_xml(engine: ModuleType, blob: bytes, mode: str) -> bytes:
    tpl = engine.DocxTemplate(io.BytesIO(blob))
    context = {'value': 'hello'}
    if mode == 'subdoc':
        context['sub'] = tpl.new_subdoc()
        context['sub'].add_paragraph('sub')
    tpl.render(context, jinja2.Environment() if mode == 'environment' else None)
    output = io.BytesIO()
    tpl.save(output)
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as package:
        return package.read('word/document.xml')


class NativeBodyNamespaceAdoptionTests(unittest.TestCase):
    def test_redundant_descendant_declarations_follow_reference(self) -> None:
        blob = template()
        for mode in ('direct', 'subdoc', 'environment'):
            with self.subTest(mode=mode):
                expected = document_xml(docxtpl, blob, mode)
                self.assertIn(b'w14:paraId="00E435F6" w14:textId="77777777"', expected)
                self.assertEqual(document_xml(docxtpl_native, blob, mode), expected)


if __name__ == '__main__':
    unittest.main()
