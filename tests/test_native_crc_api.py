"""CRC source and buffer protocols are independent of Python implementation."""
import array
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import docxtpl
import docxtpl_native

class NativeCrcApiTests(unittest.TestCase):
    def observe(self, engine, value):
        trace=[]
        class Source:
            @property
            def read(self):
                trace.append('lookup')
                def read(): trace.append('read'); return value
                return read
        try: result=engine.DocxTemplate.get_file_crc(Source()); error=None
        except Exception as exc: result=None; error=(type(exc).__name__,str(exc))
        return result,error,trace

    def test_buffer_protocol_and_errors(self):
        values=[b'',b'123456789',bytearray(b'abc'),memoryview(b'abc'),
                memoryview(b'abcdef')[::2],array.array('I',[1,2]), 'x',None,17]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(self.observe(docxtpl_native,value),self.observe(docxtpl,value))

    def test_file_paths_and_stream_position(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'input.bin'; path.write_bytes(b'prefix123456789')
            for source in (str(path),path,os.fsencode(path)):
                self.assertEqual(docxtpl_native.DocxTemplate.get_file_crc(source),docxtpl.DocxTemplate.get_file_crc(source))
            for engine in (docxtpl,docxtpl_native):
                stream=io.BytesIO(b'prefix123456789'); stream.seek(6)
                self.assertEqual(engine.DocxTemplate.get_file_crc(stream),3421780262)
                self.assertEqual(stream.tell(),15)

    def test_context_manager_error_and_exception_state(self):
        # Load the native extension before patching open, so the patch only
        # observes the input context manager, not Python imports.
        docxtpl_native._extension()
        def observe(engine, mode):
            trace=[]; original=ValueError('body'); exit_failure=LookupError('exit')
            class File:
                def read(self):
                    trace.append('read')
                    if mode != 'success': raise original
                    return b'abc'
            class Manager:
                def __enter__(self): trace.append('enter'); return File()
                def __exit__(self, typ, value, tb):
                    trace.append(('exit',typ.__name__ if typ else None,value is original,sys.exception() is original))
                    if mode=='exit_error': raise exit_failure
                    if mode=='reraise': raise value
                    if mode=='bool_error':
                        class Truth:
                            def __bool__(self):
                                trace.append(('bool',sys.exception() is original))
                                raise exit_failure
                        return Truth()
                    return mode=='suppress'
            def fake_open(path, flags): trace.append(('open',flags,flags is sys.intern('rb'))); return Manager()
            with patch('builtins.open',fake_open):
                try: result=engine.DocxTemplate.get_file_crc(object()); error=None
                except BaseException as exc:
                    result=None; error=(type(exc).__name__,str(exc),exc is original,exc is exit_failure,exc.__context__ is original)
            return result,error,trace
        for mode in ('success','propagate','suppress','exit_error','reraise','bool_error'):
            with self.subTest(mode=mode): self.assertEqual(observe(docxtpl_native,mode),observe(docxtpl,mode))

    def test_context_special_method_descriptors_and_lookup_order(self):
        docxtpl_native._extension()
        def observe(engine):
            trace=[]
            class File:
                def read(self): trace.append('read'); return b'abc'
            class Descriptor:
                def __init__(self,name): self.name=name
                def __get__(self,obj,owner):
                    trace.append(('get',self.name,obj is manager))
                    if self.name=='enter': return lambda: (trace.append('enter'),File())[1]
                    return lambda *exc: trace.append(('exit',len(exc)))
            class Manager:
                __enter__=Descriptor('enter'); __exit__=Descriptor('exit')
                def __getattribute__(self,name):
                    if name in ('__enter__','__exit__'): raise AssertionError('instance lookup')
                    return object.__getattribute__(self,name)
            manager=Manager()
            with patch('builtins.open',lambda *args:manager):
                try: result=engine.DocxTemplate.get_file_crc(object()); error=None
                except Exception as exc: result=None; error=(type(exc).__name__,str(exc))
            return result,error,trace
        self.assertEqual(observe(docxtpl_native),observe(docxtpl))

    def test_missing_context_manager_methods(self):
        docxtpl_native._extension()
        classes=[type('MissingBoth',(),{}),type('MissingEnter',(),{'__exit__':lambda *args:None}),
                 type('MissingExit',(),{'__enter__':lambda self:self})]
        def observe(engine,cls):
            with patch('builtins.open',lambda *args:cls()):
                try: engine.DocxTemplate.get_file_crc(object())
                except Exception as exc: return type(exc).__name__,str(exc)
            raise AssertionError('expected context-manager failure')
        for cls in classes:
            with self.subTest(cls=cls.__name__): self.assertEqual(observe(docxtpl_native,cls),observe(docxtpl,cls))

if __name__=='__main__': unittest.main()
