"""Traversal and identity for special values bound to a document package."""
import unittest
from unittest.mock import patch

import docxtpl_native
from docxtpl_native.bridge import collect_specials


class NativeCollectSpecialsTests(unittest.TestCase):
    def test_order_deduplication_and_cycles(self):
        class Image:pass
        class ChildImage(Image):pass
        class Subdoc:pass
        class Config:pass
        first=Image();second=ChildImage();subdoc=Subdoc();token=object();calls=[]
        cycle=[first,second,first];cycle.append(cycle)
        context={'items':cycle};config=Config();config.globals={'image':second,'subdoc':subdoc}
        def convert(value):calls.append(value);return token
        with patch.object(docxtpl_native,'InlineImage',Image),patch.object(docxtpl_native,'Subdoc',Subdoc),patch.object(docxtpl_native,'_value',convert):
            result=collect_specials(context,config)
        self.assertEqual(calls,[subdoc,second,first])
        self.assertEqual([entry['identity'] for entry in result],[id(subdoc),id(second),id(first)])
        self.assertTrue(all(entry['value'] is token for entry in result))

    def test_only_exact_builtin_containers_are_traversed(self):
        class Image:pass
        class Subdoc:pass
        class ListSubclass(list):pass
        class Opaque:
            def __iter__(self):raise AssertionError('opaque iteration')
            def __hash__(self):raise AssertionError('opaque hash')
        class Config:globals={}
        hidden=Image();visible=Image();calls=[]
        with patch.object(docxtpl_native,'InlineImage',Image),patch.object(docxtpl_native,'Subdoc',Subdoc),patch.object(docxtpl_native,'_value',lambda value:calls.append(value)):
            result=collect_specials([ListSubclass([hidden]),Opaque(),visible],Config())
        self.assertEqual(calls,[visible]);self.assertEqual(result,[{'identity':id(visible),'value':None}])

    def test_globals_and_converter_errors_preserve_identity(self):
        class Image:pass
        class Subdoc:pass
        marker=RuntimeError('globals failed');calls=[]
        class Config:
            @property
            def globals(self):calls.append('globals');raise marker
        with patch.object(docxtpl_native,'InlineImage',Image),patch.object(docxtpl_native,'Subdoc',Subdoc),patch.object(docxtpl_native,'_value',lambda value:calls.append('convert')):
            with self.assertRaises(RuntimeError) as caught:collect_specials([Image()],Config())
        self.assertIs(caught.exception,marker);self.assertEqual(calls,['globals'])

        marker=RuntimeError('convert failed');first=Image();second=Image();calls=[]
        class Config2:globals={}
        def convert(value):
            calls.append(value)
            if value is first:raise marker
            return 'ok'
        with patch.object(docxtpl_native,'InlineImage',Image),patch.object(docxtpl_native,'Subdoc',Subdoc),patch.object(docxtpl_native,'_value',convert):
            with self.assertRaises(RuntimeError) as caught:collect_specials([first,second],Config2())
        self.assertIs(caught.exception,marker);self.assertEqual(calls,[second,first])

    def test_instance_protocol_can_expose_a_special_type(self):
        class Image:pass
        class Subdoc:pass
        class Proxy:
            @property
            def __class__(self):return Image
        class Config:globals={}
        proxy=Proxy();token=object()
        with patch.object(docxtpl_native,'InlineImage',Image),patch.object(docxtpl_native,'Subdoc',Subdoc),patch.object(docxtpl_native,'_value',lambda value:token):
            result=collect_specials([proxy],Config())
        self.assertEqual(len(result),1);self.assertEqual(result[0]['identity'],id(proxy));self.assertIs(result[0]['value'],token)

    def test_real_special_values_preserve_stream_position(self):
        import base64,io
        stream=io.BytesIO(b'image-data');stream.seek(3)
        image=docxtpl_native.InlineImage(None,stream)
        subdoc=object.__new__(docxtpl_native.Subdoc);subdoc.tpl=None;subdoc.source=b'document-data'
        class Config:globals={'subdoc':subdoc}
        result=collect_specials({'image':image},Config())
        self.assertEqual([entry['identity'] for entry in result],[id(subdoc),id(image)])
        self.assertEqual([entry['value']['kind'] for entry in result],['subdoc','image'])
        self.assertEqual([base64.b64decode(entry['value']['data']) for entry in result],[b'document-data',b'image-data'])
        self.assertEqual(stream.tell(),3)


if __name__=='__main__':unittest.main()
