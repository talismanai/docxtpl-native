"""Base64 buffer and exception compatibility with the previous binding."""
import array
import base64
import random
import sys
import unittest

from docxtpl_native import _b64


class NativeBase64Tests(unittest.TestCase):
    def test_seeded_bytes_and_contiguous_buffers(self):
        rng=random.Random(20261104)
        for size in list(range(80))+[1024,8193]:
            data=rng.randbytes(size);expected=base64.b64encode(data).decode('ascii')
            for value in (data,bytearray(data),memoryview(data)):
                with self.subTest(size=size,kind=type(value).__name__):self.assertEqual(_b64(value),expected)
        values=array.array('H',[0,1,255,65535])
        self.assertEqual(_b64(values),base64.b64encode(values).decode('ascii'))

    def test_invalid_values_and_noncontiguous_views(self):
        def observe(function,value):
            try:return ('ok',function(value))
            except Exception as exc:return type(exc).__name__,exc.args
        reference=lambda value:base64.b64encode(value).decode('ascii')
        for value in (None,'text',1,memoryview(b'abcdef')[::2]):
            with self.subTest(kind=type(value).__name__):self.assertEqual(observe(_b64,value),observe(reference,value))

    def test_string_identity(self):
        def observe(function,data,interned):
            first=function(data);second=function(data)
            return first,first is second,first is interned
        reference=lambda value:base64.b64encode(value).decode('ascii')
        for data in (b'',b'a',b'hello world'):
            interned=sys.intern(reference(data))
            with self.subTest(data=data):self.assertEqual(observe(_b64,data,interned),observe(reference,data,interned))

    @unittest.skipUnless(sys.version_info >= (3,12),'Python-level buffer protocol requires 3.12')
    def test_python_buffer_lifecycle_and_exception_identity(self):
        def run(function,fail):
            trace=[];marker=RuntimeError('buffer failed')
            class Exporter:
                def __buffer__(self,flags):
                    trace.append(('get',flags))
                    if fail:raise marker
                    return memoryview(b'hello')
                def __release_buffer__(self,view):trace.append(('release',bytes(view)))
            try:result=function(Exporter());error=None
            except Exception as exc:result=None;error=(type(exc).__name__,exc.args,exc is marker)
            return result,error,trace
        reference=lambda value:base64.b64encode(value).decode('ascii')
        for fail in (False,True):
            with self.subTest(fail=fail):self.assertEqual(run(_b64,fail),run(reference,fail))


if __name__=='__main__':unittest.main()
