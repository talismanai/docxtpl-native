"""Save phases, timestamps and repeated ZIP replacements match the reference."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.part import Part
from docx.opc.packuri import PackURI
import docxtpl
import docxtpl_native


def source_document():
    doc = Document()
    doc.add_paragraph('Hello {{ name }}')
    for name, kind in [('word/media/opaque.png', RT.IMAGE), ('word/embeddings/data.bin', RT.OLE_OBJECT)]:
        part = Part(PackURI('/' + name), 'application/octet-stream', b'original', doc.part.package)
        doc.part.relate_to(part, kind)
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()


class NativeSaveReplacementTests(unittest.TestCase):
    def test_clock_failure_retains_original_output_and_read_cursor(self):
        source = source_document()
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            count = len(archive.infolist())
            reordered = io.BytesIO()
            with zipfile.ZipFile(reordered, 'w') as target:
                for item in reversed(archive.infolist()):
                    target.writestr(item, archive.read(item))
        def run(engine, direct, fail_at):
            tpl = engine.DocxTemplate(io.BytesIO(source)); tpl.render({'name': 'Ada'})
            tpl.zipname_to_replace['word/media/opaque.png'] = b'new'
            output = io.BytesIO(reordered.getvalue() if direct else b'')
            failure = ValueError('clock failed'); calls = 0
            def clock():
                nonlocal calls
                calls += 1
                if calls == fail_at: raise failure
                return 1700000000
            with patch('zipfile.time.time', side_effect=clock):
                try:
                    (tpl.post_processing if direct else tpl.save)(output)
                except ValueError as error:
                    same = error is failure
                else: raise AssertionError('expected clock failure')
            result = same, calls, output.tell(), tpl.is_saved, output.getvalue()
            # Release reference resources after observing the failure and while
            # their destination is still open, avoiding unrelated destructor IO.
            failure.__traceback__ = None
            return result
        for direct, fail_at in [(False, 2), (False, count + 2), (True, 2)]:
            with self.subTest(direct=direct, fail_at=fail_at):
                self.assertEqual(run(docxtpl_native, direct, fail_at), run(docxtpl, direct, fail_at))

    def test_file_and_stream_bytes_clock_calls_and_position(self):
        source = source_document()
        for mode in ['file', 'stream']:
            for family in ['zipname', 'media', 'embedded']:
                def run(engine, directory):
                    tpl = engine.DocxTemplate(io.BytesIO(source))
                    tpl.render({'name': 'Ada'})
                    original = Path(directory) / 'original'; original.write_bytes(b'original')
                    replacement = Path(directory) / 'replacement'; replacement.write_bytes(b'replaced')
                    if family == 'zipname':
                        tpl.replace_zipname('word/media/opaque.png', str(replacement))
                    else:
                        getattr(tpl, 'replace_' + family)(str(original), str(replacement))
                    target = str(Path(directory) / (engine.__name__ + '.docx')) if mode == 'file' else io.BytesIO()
                    with patch('zipfile.time.time', return_value=1700000000) as clock:
                        tpl.save(target)
                    data = Path(target).read_bytes() if mode == 'file' else target.getvalue()
                    return data, clock.call_count, None if mode == 'file' else target.tell(), tpl.is_saved
                with self.subTest(mode=mode, family=family), tempfile.TemporaryDirectory() as directory:
                    self.assertEqual(run(docxtpl_native, directory), run(docxtpl, directory))

    def test_repeated_crc_replacements_use_the_original_rendered_package(self):
        source = source_document()
        def run(engine, directory):
            original = Path(directory) / 'original'; original.write_bytes(b'original')
            replacement = Path(directory) / 'replacement'
            tpl = engine.DocxTemplate(io.BytesIO(source))
            replacement.write_bytes(b'first')
            tpl.replace_media(str(original), str(replacement))
            tpl.render({'name': 'Ada'})
            observed = []
            for content in [b'first', b'second']:
                replacement.write_bytes(content)
                tpl.replace_media(str(original), str(replacement))
                output = io.BytesIO()
                with patch('zipfile.time.time', return_value=1700000000):
                    tpl.save(output)
                with zipfile.ZipFile(output) as archive:
                    observed.append(archive.read('word/media/opaque.png'))
            # ZIP postprocessing does not replace the in-memory document blob.
            observed.append(next(rel.target_part.blob for rel in tpl.docx.part.rels.values()
                                 if not rel.is_external and str(rel.target_part.partname) == '/word/media/opaque.png'))
            return observed
        with tempfile.TemporaryDirectory() as directory:
            expected = run(docxtpl, directory)
            self.assertEqual(expected, [b'first', b'second', b'original'])
            self.assertEqual(run(docxtpl_native, directory), expected)

    def test_postprocessing_override_sees_written_unreplaced_output(self):
        source = source_document()
        def run(engine):
            tpl = engine.DocxTemplate(io.BytesIO(source)); tpl.render({'name': 'Ada'})
            tpl.zipname_to_replace['word/media/opaque.png'] = b'new'
            output = io.BytesIO(); observed = []; failure = ValueError('postprocess failed')
            def postprocess(target):
                with zipfile.ZipFile(io.BytesIO(target.getvalue())) as archive:
                    observed.append((target is output, tpl.is_saved, archive.read('word/media/opaque.png')))
                raise failure
            tpl.post_processing = postprocess
            with patch('zipfile.time.time', return_value=1700000000):
                try: tpl.save(output)
                except Exception as error: observed.append((error is failure, tpl.is_saved))
                else: observed.append('no error')
            return observed
        expected = run(docxtpl)
        self.assertEqual(expected, [(True, False, b'original'), (True, False)])
        self.assertEqual(run(docxtpl_native), expected)
