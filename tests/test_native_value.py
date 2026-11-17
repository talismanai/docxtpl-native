"""Typed payload conversion and caller-visible Python protocols."""
import base64
import io
import math
import sys
import unittest

from docxtpl_native import InlineImage,Listing,RichText,Subdoc,UnsupportedFeature,_value


class NativeValueTests(unittest.TestCase):
    def test_scalars_boundaries_and_identity(self):
        text='uninterned text '+str(id(self))
        cases=[(None,{'kind':'none'}),(True,{'kind':'bool','value':True}),
               (-(2**127),{'kind':'int','value':str(-(2**127))}),
               (2**127-1,{'kind':'int','value':str(2**127-1)}),
               (1.25,{'kind':'float','value':1.25}),(text,{'kind':'str','value':text})]
        for value,expected in cases:
            with self.subTest(value=value):
                result=_value(value);self.assertEqual(result,expected)
                self.assertIs(result['kind'],sys.intern(expected['kind']))
        self.assertIs(_value(text)['value'],text)

    def test_nested_containers_errors_and_shared_acyclic_value(self):
        shared=[1];result=_value({'first':shared,'second':shared})
        self.assertEqual(result,{'kind':'map','items':[['first',{'kind':'list','items':[{'kind':'int','value':'1'}]}],['second',{'kind':'list','items':[{'kind':'int','value':'1'}]}]]})
        loop=[];loop.append(loop)
        cases=[(2**127,'integer outside i128 range'),(-(2**127)-1,'integer outside i128 range'),
               (math.inf,'non-finite float'),(math.nan,'non-finite float'),(loop,'cyclic context'),
               ({1:'value'},'non-string mapping key'),((1,),'context type builtins.tuple')]
        for value,message in cases:
            with self.subTest(message=message),self.assertRaisesRegex(UnsupportedFeature,message):_value(value)
        with self.assertRaisesRegex(UnsupportedFeature,'context depth above 100'):_value(None,depth=101)
        nested=None
        for _ in range(101):nested=[nested]
        active=set()
        with self.assertRaisesRegex(UnsupportedFeature,'context depth above 100'):_value(nested,active=active)
        self.assertEqual(active,set())

    def test_special_values_and_stream_position(self):
        stream=io.BytesIO(b'image bytes');stream.seek(3)
        image=InlineImage(None,stream,width=42,height=84)
        encoded=_value(image)
        self.assertEqual(encoded['kind'],'image');self.assertEqual(base64.b64decode(encoded['data']),b'image bytes')
        self.assertEqual((encoded['filename'],encoded['width'],encoded['height']),(None,42,84));self.assertEqual(stream.tell(),3)
        subdoc=object.__new__(Subdoc);subdoc.tpl=None;subdoc.source=b'document bytes'
        self.assertEqual(base64.b64decode(_value(subdoc)['data']),b'document bytes')
        rich=RichText('hello');self.assertEqual(_value(rich),{'kind':'xml','value':str(rich)})
        listing=Listing('hello');self.assertEqual(_value(listing),{'kind':'xml','value':str(listing)})

    def test_active_protocol_and_cleanup_exception_context(self):
        trace=[];original=RuntimeError('conversion failed');cleanup=RuntimeError('cleanup failed')
        class Active:
            def __contains__(self,value):trace.append(('contains',value));return False
            def add(self,value):trace.append(('add',value))
            def remove(self,value):trace.append(('remove',value));raise cleanup
        class BrokenRichText(RichText):
            def __str__(self):raise original
        root=[BrokenRichText('hello')]
        with self.assertRaises(RuntimeError) as caught:_value(root,active=Active())
        self.assertIs(caught.exception,cleanup);self.assertIs(caught.exception.__context__,original)
        self.assertEqual(trace,[('contains',id(root)),('add',id(root)),('remove',id(root))])

    def test_container_mutation_during_conversion(self):
        values=[]
        class AppendingRichText(RichText):
            def __str__(self):values.append('late');return 'xml'
        values.append(AppendingRichText('x'))
        self.assertEqual(_value(values),{'kind':'list','items':[{'kind':'xml','value':'xml'},{'kind':'str','value':'late'}]})
        mapping={}
        class MutatingRichText(RichText):
            def __str__(self):mapping['late']='value';return 'xml'
        mapping['first']=MutatingRichText('x');active=set()
        with self.assertRaises(RuntimeError):_value(mapping,active=active)
        self.assertEqual(active,set())

    def test_rebound_type_and_float_checker_are_observed(self):
        import builtins
        from unittest.mock import patch
        import docxtpl_native as module
        trace=[]
        def traced_type(value):trace.append(builtins.type(value).__name__);return builtins.type(value)
        with patch.dict(module.__dict__,{'type':traced_type}):
            self.assertEqual(_value({'k':1}),{'kind':'map','items':[['k',{'kind':'int','value':'1'}]]})
        self.assertEqual(trace,['dict','dict','dict','dict','dict','dict','str','int','int'])
        calls=[]
        with patch.object(module.math,'isfinite',lambda value:calls.append(value) or False):
            with self.assertRaisesRegex(UnsupportedFeature,'non-finite float'):_value(1.25)
        self.assertEqual(calls,[1.25])

    def test_rebound_recursive_converter_is_called(self):
        from unittest.mock import patch
        import docxtpl_native as module
        token=object();calls=[]
        def convert(value,*,depth,active):
            calls.append((value,depth,isinstance(active,set)));return token
        with patch.dict(module.__dict__,{'_value':convert}):result=_value([1])
        self.assertEqual(calls,[(1,1,True)]);self.assertIs(result['items'][0],token)

    def test_rebound_globals_name_does_not_change_conversion(self):
        from unittest.mock import patch
        import docxtpl_native as module
        def unexpected():raise AssertionError('globals was called')
        with patch.dict(module.__dict__,{'globals':unexpected}):
            self.assertEqual(_value(None),{'kind':'none'})

    def test_rebinding_module_builtins_does_not_replace_function_builtins(self):
        from unittest.mock import patch
        import docxtpl_native as module
        with patch.dict(module.__dict__,{'__builtins__':{}}):
            self.assertEqual(_value(None),{'kind':'none'})


if __name__=='__main__':unittest.main()
