"""ZIP replacement retains the reference's entry compression methods."""
import io
import hashlib
import unittest
import zipfile
import zlib
from PIL import Image
from docx import Document
import docxtpl
import docxtpl_native

def image(color):
    out = io.BytesIO(); Image.new('RGB', (3, 3), color).save(out, format='PNG'); return out.getvalue()

class NativePostprocessingZipTests(unittest.TestCase):
    def test_save_preserves_replacement_precedence_and_compression(self):
        red, green, blue, yellow, purple = map(image, ('red', 'green', 'blue', 'yellow', 'purple'))
        doc = Document()
        for blob in (red, green, purple): doc.add_paragraph().add_run().add_picture(io.BytesIO(blob))
        original = io.BytesIO(); doc.save(original); source = original.getvalue()
        def save(engine):
            tpl = engine.DocxTemplate(io.BytesIO(source))
            tpl.crc_to_new_media = {zlib.crc32(red): yellow, zlib.crc32(green): blue}
            tpl.zipname_to_replace = {'word/media/image1.png': blue}
            out = io.BytesIO(); tpl.save(out)
            with zipfile.ZipFile(io.BytesIO(out.getvalue())) as archive:
                return [(item.filename, hashlib.sha256(archive.read(item)).hexdigest(), item.compress_type) for item in archive.infolist()]
        expected = save(docxtpl); actual = save(docxtpl_native)
        self.assertEqual(actual, expected)
        media = {name: blob for name, blob, _ in actual if name.startswith('word/media/')}
        blue_hash = hashlib.sha256(blue).hexdigest()
        self.assertEqual(media, {'word/media/image1.png': blue_hash, 'word/media/image2.png': blue_hash,
                                 'word/media/image3.png': hashlib.sha256(purple).hexdigest()})

if __name__ == '__main__': unittest.main()
