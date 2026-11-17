"""Direct ZIP post-processing compared with the original public API."""
import hashlib
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
import docxtpl
import docxtpl_native
from paritylab.api_spec_probes import PROBES

def parts(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return [(item.filename, hashlib.sha256(archive.read(item)).hexdigest(), item.compress_type, item.is_dir()) for item in archive.infolist()]

class NativePostProcessingTests(unittest.TestCase):
    def test_direct_reference_partitions(self):
        names=('api_postprocess_no_replacements','api_postprocess_stream_replacements',
               'api_postprocess_directory_path','api_postprocess_invalid_path')
        for name in names:
            with self.subTest(name=name):
                probe=PROBES[name]; expected=probe.execute(docxtpl); actual=probe.execute(docxtpl_native)
                self.assertEqual(actual.observation,expected.observation)
                self.assertEqual(actual.observation,probe.expected)
                self.assertEqual(actual.outputs.keys(),expected.outputs.keys())
                for key in actual.outputs: self.assertEqual(parts(actual.outputs[key]),parts(expected.outputs[key]))

    def test_mapping_callback_error_preserves_partial_zip(self):
        def observe(engine):
            trace=[]; failure=RuntimeError('mapping failed')
            source=io.BytesIO()
            with zipfile.ZipFile(source,'w') as archive:
                archive.writestr('first',b'old'); archive.writestr('second',b'old')
            class Mapping:
                def __bool__(self): trace.append('bool'); return True
                def __contains__(self,key):
                    trace.append(('contains',key))
                    if key=='second': raise failure
                    return key=='first'
                def __getitem__(self,key): trace.append(('get',key)); return b'new'
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'data.zip';path.write_bytes(source.getvalue())
                tpl=engine.DocxTemplate(None);tpl.zipname_to_replace=Mapping()
                try: tpl.post_processing(path)
                except RuntimeError as error: same=error is failure
                else: raise AssertionError('expected mapping failure')
                with zipfile.ZipFile(path) as archive:
                    entries=[(item.filename,archive.read(item),item.compress_type) for item in archive.infolist()]
                temporary=Path(str(path)+'_docxtpl_before_replace_medias')
                return same,trace,entries,temporary.exists()
        expected=observe(docxtpl);actual=observe(docxtpl_native)
        self.assertEqual(actual,expected)
        self.assertEqual(actual[2],[('first',b'new',zipfile.ZIP_STORED)])

    def test_string_and_buffer_replacement_values(self):
        def observe(engine):
            source=io.BytesIO()
            with zipfile.ZipFile(source,'w') as archive:
                archive.writestr('text',b'old');archive.writestr('buffer',b'old')
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'data.zip';path.write_bytes(source.getvalue())
                tpl=engine.DocxTemplate(None);tpl.zipname_to_replace={'text':'café','buffer':memoryview(b'new')}
                tpl.post_processing(path)
                with zipfile.ZipFile(path) as archive: return archive.read('text'),archive.read('buffer')
        self.assertEqual(observe(docxtpl_native),observe(docxtpl))
        self.assertEqual(observe(docxtpl_native),('café'.encode(),b'new'))

    def test_invalid_and_non_docx_stream_errors_and_position(self):
        archives=[b'invalid',b'']
        for comment,with_entry in ((b'',False),(b'',True),(b'comment',True)):
            source=io.BytesIO()
            with zipfile.ZipFile(source,'w') as archive:
                if with_entry: archive.writestr('other',b'data')
                archive.comment=comment
            archives.append(source.getvalue())
        def observe(engine,data):
            stream=io.BytesIO(data);stream.seek(2);tpl=engine.DocxTemplate(None);tpl.zipname_to_replace={'unused':b'x'}
            try: tpl.post_processing(stream)
            except Exception as error: return type(error).__name__,str(error),stream.tell(),stream.getvalue()
            raise AssertionError('expected failure')
        for archive in archives:
            with self.subTest(size=len(archive)):
                self.assertEqual(observe(docxtpl_native,archive),observe(docxtpl,archive))

    def test_crc_mapping_key_identity(self):
        def observe(engine):
            seen=[];source=io.BytesIO()
            with zipfile.ZipFile(source,'w') as archive:
                archive.writestr('word/media/a.png',b'abc')
                archive.writestr('word/embeddings/a.bin',b'def')
            class Mapping:
                def __init__(self,name): self.name=name; self.last=None
                def __bool__(self): return True
                def __contains__(self,key): self.last=key; return True
                def __getitem__(self,key): seen.append((self.name,key is self.last)); return b'new'
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'data.zip';path.write_bytes(source.getvalue())
                tpl=engine.DocxTemplate(None);tpl.crc_to_new_media=Mapping('media');tpl.crc_to_new_embedded=Mapping('embedded')
                tpl.post_processing(path)
                with zipfile.ZipFile(path) as archive: values=[archive.read(item) for item in archive.infolist()]
                return seen,values
        actual=observe(docxtpl_native)
        self.assertEqual(actual,observe(docxtpl))
        self.assertEqual(actual,([('media',True),('embedded',True)],[b'new',b'new']))

    def test_string_encoding_literal_identity(self):
        def observe(engine):
            encodings=[];source=io.BytesIO();interned=sys.intern('utf-8')
            with zipfile.ZipFile(source,'w') as archive:
                archive.writestr('first',b'old'); archive.writestr('second',b'old')
            class Text(str):
                def encode(self,encoding): encodings.append(encoding); return super().encode(encoding)
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'data.zip';path.write_bytes(source.getvalue())
                tpl=engine.DocxTemplate(None);tpl.zipname_to_replace={'first':Text('x'),'second':Text('y')}
                tpl.post_processing(path)
            return len(encodings),encodings[0] is encodings[1],encodings[0] is interned
        self.assertEqual(observe(docxtpl_native),observe(docxtpl))

    def test_replacement_maps_are_looked_up_again(self):
        def observe(engine,mode):
            trace=[];source=io.BytesIO()
            with zipfile.ZipFile(source,'w') as archive: archive.writestr('word/media/a.png',b'abc')
            class Template(engine.DocxTemplate):
                def __getattribute__(self,name):
                    if name in ('zipname_to_replace','crc_to_new_media','crc_to_new_embedded'): trace.append(name)
                    return super().__getattribute__(name)
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'data.zip';path.write_bytes(source.getvalue());tpl=Template(None)
                if mode=='name': tpl.zipname_to_replace={'word/media/a.png':b'new'}
                else:
                    import zlib
                    tpl.crc_to_new_media={zlib.crc32(b'abc'):b'new'}
                trace.clear();tpl.post_processing(path)
                with zipfile.ZipFile(path) as archive: value=archive.read('word/media/a.png')
                return trace,value
        for mode in ('name','crc'):
            with self.subTest(mode=mode): self.assertEqual(observe(docxtpl_native,mode),observe(docxtpl,mode))

if __name__=='__main__': unittest.main()
