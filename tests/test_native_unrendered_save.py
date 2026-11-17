"""Saved core properties follow python-docx load and assignment state."""
import io
import struct
import unittest
import zipfile
import zlib
from types import ModuleType

from docx import Document
import docxtpl
import docxtpl_native
from jinja2.exceptions import TemplateSyntaxError

CORE = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    '<dc:creator></dc:creator><dc:title></dc:title><dc:language>en-US</dc:language>'
    '<dcterms:created xsi:type="dcterms:W3CDTF">2018-08-06T21:44:20Z</dcterms:created>'
    '</cp:coreProperties>')


def png(red: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    pixel = zlib.compress(bytes([0, red, 0, 0]))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
        + chunk(b'IDAT', pixel) + chunk(b'IEND', b''))


def template(body: str) -> bytes:
    doc = Document()
    doc.add_paragraph(body)
    doc.add_picture(io.BytesIO(png(0)))
    source = io.BytesIO()
    doc.save(source)
    output = io.BytesIO()
    # Producer-written core properties: double-quoted declaration, explicit end tags.
    with zipfile.ZipFile(io.BytesIO(source.getvalue())) as old, zipfile.ZipFile(output, 'w') as new:
        for item in old.infolist():
            new.writestr(item, CORE if item.filename == 'docProps/core.xml' else old.read(item))
    return output.getvalue()


def saved_parts(engine: ModuleType, blob: bytes, render: str | None) -> dict[str, bytes]:
    tpl = engine.DocxTemplate(io.BytesIO(blob))
    if render == 'failed':
        with unittest.TestCase().assertRaises(TemplateSyntaxError):
            tpl.render({'value': 'x'})
    elif render == 'resaved':
        tpl.render({'value': 'x'})
        # A replacement registered after render makes the native save
        # process the rendered bytes again.
        tpl.replace_pic('image.png', io.BytesIO(png(255)))
    output = io.BytesIO()
    tpl.save(output)
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as package:
        return {name: package.read(name) for name in package.namelist()}


class NativeUnrenderedSaveTests(unittest.TestCase):
    def test_parts_match_reference_without_completed_render(self) -> None:
        for render, body, creator in ((None, '{{ value }}', b'<dc:creator/>'),
                                      ('failed', '{{ value ', b'<dc:creator/>'),
                                      ('resaved', '{{ value }}', b'<dc:creator></dc:creator>')):
            with self.subTest(render=render):
                blob = template(body)
                expected = saved_parts(docxtpl, blob, render)
                self.assertIn(creator, expected['docProps/core.xml'])
                self.assertEqual(saved_parts(docxtpl_native, blob, render), expected)


if __name__ == '__main__':
    unittest.main()
