"""Observable protocols for the public media replacement methods."""
import importlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import docxtpl
import docxtpl_native


class ReplacementMethodTests(unittest.TestCase):
    def test_real_files_and_streams(self):
        def run(engine, folder):
            source = Path(folder) / 'source.bin'; source.write_bytes(b'original')
            target = Path(folder) / 'target.bin'; target.write_bytes(b'replacement')
            tpl = engine.DocxTemplate(None)
            source_stream = io.BytesIO(b'xoriginal'); source_stream.seek(1)
            target_stream = io.BytesIO(b'xreplacement'); target_stream.seek(1)
            tpl.replace_media(source_stream, target_stream)
            tpl.replace_embedded(source, target)
            tpl.replace_pic('image.png', io.BytesIO(b'replacement'))
            tpl.replace_zipname('word/item.bin', target)
            return (tpl.crc_to_new_media, tpl.crc_to_new_embedded, tpl.pics_to_replace,
                    tpl.zipname_to_replace, source_stream.tell(), target_stream.tell())
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(run(docxtpl_native, folder), run(docxtpl, folder))

    def test_context_manager_order_suppression_and_failure_identity(self):
        def run(engine, method, fail_at, suppress):
            module = importlib.import_module('docxtpl.template' if engine is docxtpl else 'docxtpl_native')
            trace = []; marker = RuntimeError('primary'); exit_error = LookupError('exit failed')
            source, target, value, key = object(), object(), object(), object()
            mapping_name = {'replace_media':'crc_to_new_media', 'replace_pic':'pics_to_replace',
                            'replace_embedded':'crc_to_new_embedded', 'replace_zipname':'zipname_to_replace'}[method]
            class Mapping:
                def __setitem__(self, actual_key, actual_value):
                    trace.append(('set', actual_key is key, actual_value is value))
                    if fail_at == 'set': raise marker
            class Template(engine.DocxTemplate):
                def __getattribute__(self, name):
                    if name == mapping_name: trace.append('mapping')
                    return super().__getattribute__(name)
                def get_file_crc(self, actual):
                    trace.append(('crc', actual is source))
                    if fail_at == 'crc': raise marker
                    return key
            tpl = Template(None);setattr(tpl, mapping_name, Mapping());trace.clear()
            class File:
                def read(self):
                    trace.append('read')
                    if fail_at in ('read', 'read_and_exit'): raise marker
                    return value
            class Manager:
                def __enter__(self):
                    trace.append('enter')
                    if fail_at == 'enter': raise marker
                    return File()
                def __exit__(self, typ, error, tb):
                    trace.append(('exit', typ is None, error is marker,
                                  sys.exception() is marker if error is not None else sys.exception() is None))
                    if fail_at in ('exit', 'read_and_exit'): raise exit_error
                    return suppress
            def opener(actual, mode):
                trace.append(('open', actual is target, mode))
                return Manager()
            with patch.object(module, 'open', opener, create=True):
                try:
                    result = getattr(tpl, method)(source if method in ('replace_media','replace_embedded') else key, target)
                    error = None
                except BaseException as exc:
                    result = None
                    error = (type(exc).__name__, str(exc), exc is marker, exc is exit_error,
                             exc.__context__ is marker)
            return result, error, trace
        for method in ('replace_media', 'replace_pic', 'replace_embedded', 'replace_zipname'):
            for failure, suppress in ((None, False), ('enter', False), ('read', False), ('read', True), ('read_and_exit', False), ('set', False), ('exit', False)):
                with self.subTest(method=method, failure=failure, suppress=suppress):
                    self.assertEqual(run(docxtpl_native, method, failure, suppress), run(docxtpl, method, failure, suppress))
            if method in ('replace_media','replace_embedded'):
                with self.subTest(method=method, failure='crc'):
                    self.assertEqual(run(docxtpl_native, method, 'crc', False), run(docxtpl, method, 'crc', False))

    def test_stream_read_property_and_dynamic_mapping_order(self):
        def run(engine, method):
            trace = []; key, value = object(), object()
            mapping_name = 'crc_to_new_media' if method == 'replace_media' else 'pics_to_replace'
            class Stream:
                @property
                def read(self):
                    trace.append('read_lookup')
                    def read(): trace.append('read'); return value
                    return read
            class Mapping:
                def __setitem__(self, actual_key, actual_value):trace.append(('set', actual_key is key, actual_value is value))
            class Template(engine.DocxTemplate):
                def __getattribute__(self, name):
                    if name == mapping_name: trace.append('mapping')
                    return super().__getattribute__(name)
                def get_file_crc(self, source):trace.append('crc');return key
            tpl = Template(None);setattr(tpl, mapping_name, Mapping());trace.clear()
            result = getattr(tpl, method)(object() if method == 'replace_media' else key, Stream())
            return result, trace
        for method in ('replace_media', 'replace_pic'):
            with self.subTest(method=method):self.assertEqual(run(docxtpl_native, method), run(docxtpl, method))

    def test_rebound_hasattr_is_used(self):
        def run(engine, method):
            module = importlib.import_module('docxtpl.template' if engine is docxtpl else 'docxtpl_native')
            trace=[]; destination=io.BytesIO(b'data'); tpl=engine.DocxTemplate(None)
            tpl.get_file_crc=lambda source: 7
            def has(value, name):
                trace.append((value is destination, name))
                return True
            with patch.object(module, 'hasattr', has, create=True):
                getattr(tpl,method)(object() if method=='replace_media' else 'image',destination)
            return trace
        for method in ('replace_media','replace_pic'):
            with self.subTest(method=method):self.assertEqual(run(docxtpl_native,method),run(docxtpl,method))

    def test_base_exceptions_and_suppression(self):
        def run(engine, error_type, suppress):
            module=importlib.import_module('docxtpl.template' if engine is docxtpl else 'docxtpl_native')
            error=error_type('stop'); trace=[]
            class Manager:
                def __enter__(self): return self
                def read(self): raise error
                def __exit__(self, typ, value, tb):
                    trace.append((typ is error_type, value is error, sys.exception() is error))
                    return suppress
            with patch.object(module,'open',lambda *args:Manager(),create=True):
                try:
                    result=engine.DocxTemplate(None).replace_zipname('item','target')
                except BaseException as actual:
                    return trace, type(actual), actual is error
            return trace, result, None
        for error_type in (KeyboardInterrupt,SystemExit):
            for suppress in (False,True):
                with self.subTest(error=error_type.__name__,suppress=suppress):
                    self.assertEqual(run(docxtpl_native,error_type,suppress),run(docxtpl,error_type,suppress))


if __name__ == '__main__':
    unittest.main()
