"""Corrupt compressed media must not silently survive a native DOCX cycle."""
import io
import struct
import unittest
import zipfile

from docx import Document
from PIL import Image
from docxtpl_native import _native


def document_with_image():
    image = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(image, format="PNG")
    image.seek(0)
    document = Document()
    document.add_picture(image)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def damage_media_crc(blob):
    data = bytearray(blob)
    end = data.rfind(b"PK\x05\x06")
    offset = struct.unpack_from("<I", data, end + 16)[0]
    while data[offset:offset + 4] == b"PK\x01\x02":
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", data, offset + 28)
        name = bytes(data[offset + 46:offset + 46 + name_len])
        if name == b"word/media/image1.png":
            crc = struct.unpack_from("<I", data, offset + 16)[0]
            struct.pack_into("<I", data, offset + 16, crc ^ 1)
            return bytes(data)
        offset += 46 + name_len + extra_len + comment_len
    raise AssertionError("image entry missing")


class ZipIntegrityTests(unittest.TestCase):
    def test_corrupt_image_crc_is_rejected(self):
        valid = document_with_image()
        _native.process_docx(valid, "{}", False, False)
        corrupt = damage_media_crc(valid)
        with self.assertRaises(zipfile.BadZipFile) as reference:
            Document(io.BytesIO(corrupt))
        with self.assertRaises(zipfile.BadZipFile) as candidate:
            _native.process_docx(corrupt, "{}", False, False)
        self.assertEqual(str(candidate.exception), str(reference.exception))


if __name__ == "__main__":
    unittest.main()
