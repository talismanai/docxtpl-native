"""The native document package: parts, relationships and images without python-docx."""

import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import docx
import docxtpl_native
from paritylab.protocol_base import template

HYPERLINK = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink'
IMAGE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/image'
PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4'
       b'\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82')


def saved(document: docxtpl_native.DocxTemplate) -> bytes:
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


class NativePackageTests(unittest.TestCase):
    def test_reads_template_parts_and_relationships(self) -> None:
        document = docxtpl_native.DocxTemplate(io.BytesIO(template('Hi {{ name }}')))
        package = document.open_package()
        self.assertIn('word/document.xml', package.names())
        self.assertIn('word/document.xml', package)
        self.assertIn(b'{{ name }}', package.read('word/document.xml'))
        relationships = package.relationships('word/document.xml')
        self.assertTrue(all(len(item) == 4 for item in relationships))
        self.assertTrue(any(target == 'styles.xml' for _, _, target, _ in relationships))
        with self.assertRaises(KeyError):
            package.read('word/missing.xml')
        self.assertIsNone(document._document)

    def test_rendered_document_change_is_saved(self) -> None:
        document = docxtpl_native.DocxTemplate(io.BytesIO(template('Hi {{ name }}')))
        document.render({'name': 'Ada'})
        package = document.open_package()
        part = package.read('word/document.xml')
        self.assertIn(b'Hi Ada', part)
        package.write('word/document.xml', part.replace(b'Hi Ada', b'Hello Ada'))
        document.commit_package(package)
        output = saved(document)
        self.assertEqual(docx.Document(io.BytesIO(output)).paragraphs[0].text, 'Hello Ada')
        # The python-docx model, built afterwards, sees the committed bytes.
        self.assertEqual(document.docx.paragraphs[0].text, 'Hello Ada')

    def test_template_change_is_rendered(self) -> None:
        document = docxtpl_native.DocxTemplate(io.BytesIO(template('Hi {{ name }}')))
        package = document.open_package()
        part = package.read('word/document.xml')
        package.write('word/document.xml', part.replace(b'{{ name }}', b'{{ name|upper }}'))
        document.commit_package(package)
        document.render({'name': 'Ada'})
        self.assertEqual(docx.Document(io.BytesIO(saved(document))).paragraphs[0].text, 'Hi ADA')

    def test_relationships_images_and_new_parts(self) -> None:
        document = docxtpl_native.DocxTemplate(io.BytesIO(template('Hi')))
        document.render({})
        package = document.open_package()
        link = package.relate('word/document.xml', HYPERLINK, 'https://example.com', external=True)
        image = package.add_image(PNG, 'PNG', 'image/png')
        self.assertTrue(image.startswith('word/media/image') and image.endswith('.png'))
        self.assertEqual(package.add_image(PNG, 'png', 'image/png'), image)
        picture = package.relate('word/document.xml', IMAGE, image.removeprefix('word/'))
        document.commit_package(package)
        with zipfile.ZipFile(io.BytesIO(saved(document))) as archive:
            rels = archive.read('word/_rels/document.xml.rels').decode()
            self.assertIn(f'Id="{link}"', rels)
            self.assertIn('TargetMode="External"', rels)
            self.assertIn(f'Id="{picture}"', rels)
            self.assertEqual(archive.read(image), PNG)
            self.assertIn('Extension="png"', archive.read('[Content_Types].xml').decode())
        reopened = docx.Document(io.BytesIO(saved(document)))
        self.assertEqual(reopened.part.rels[link].target_ref, 'https://example.com')

    def test_pending_replacements_still_apply(self) -> None:
        document = docxtpl_native.DocxTemplate(io.BytesIO(template('Hi')))
        document.render({})
        package = document.open_package()
        image = package.add_image(PNG, 'png', 'image/png')
        # Saving with replacements walks relationships, as python-docx does.
        package.relate('word/document.xml', IMAGE, image.removeprefix('word/'))
        document.commit_package(package)
        replacement = PNG[:-12] + b'\x00\x00\x00\x00IEND\xaeB`\x82'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'replacement.png'
            path.write_bytes(replacement)
            document.replace_zipname(image, str(path))
            output = saved(document)
        with zipfile.ZipFile(io.BytesIO(output)) as archive:
            self.assertEqual(archive.read(image), replacement)

    def test_exposed_model_owns_the_document(self) -> None:
        document = docxtpl_native.DocxTemplate(io.BytesIO(template('Hi')))
        package = document.open_package()
        document.get_docx()
        for operation in (document.open_package, lambda: document.commit_package(package)):
            with self.subTest(operation=operation), self.assertRaises(docxtpl_native.UnsupportedFeature):
                operation()
        with self.assertRaises(TypeError):
            docxtpl_native.DocxTemplate(io.BytesIO(template('Hi'))).commit_package(object())

    def test_invalid_names_and_extensions(self) -> None:
        package = docxtpl_native.DocxTemplate(io.BytesIO(template('Hi'))).open_package()
        for name in ('', '/word/x.xml', 'word/'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                package.write(name, b'x')
        with self.assertRaises(ValueError):
            package.add_image(PNG, 'p.ng', 'image/png')
        with self.assertRaises(KeyError):
            package.relate('word/missing.xml', HYPERLINK, 'x', external=True)


if __name__ == '__main__':
    unittest.main()
