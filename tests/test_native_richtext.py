"""Differential rich-value construction and gates against reference algorithms."""
import unittest
from unittest.mock import patch
import docxtpl
import docxtpl_native


class NativeRichValueTests(unittest.TestCase):
    def test_property_protocols_and_conversion_order(self):
        def run(engine):
            events=[]
            class Property:
                def __init__(self,name):self.name=name
                def __bool__(self):events.append(('bool',self.name));return True
                def __str__(self):events.append(('str',self.name));return self.name
                def __format__(self,spec):events.append(('format',self.name,spec));return self.name
                def __eq__(self,other):events.append(('eq',self.name,other));return other=='wave'
                def __getitem__(self,key):events.append(('item',self.name,repr(key)));return '#' if key==0 else self.name
                def __contains__(self,value):events.append(('contains',self.name,value));return True
                def split(self,*args):
                    events.append(('split',self.name,args))
                    return iter([Property('eastAsia'),Property('Arial')])
            class Text(str):
                def replace(self,*args):events.append(('replace',args));return Text(super().replace(*args))
            value=engine.RichText()
            kwargs={name:Property(name) for name in ['style','font','color','highlight','size','subscript','superscript','bold','italic','underline','strike','rtl','lang','url_id']}
            value.add(Text('<&>'),**kwargs)
            return value.xml,events
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_unusual_scalars_surrogates_and_tuple_properties(self):
        values=(None,False,0,3,2**100,-1,1.5,b'\xffok','\ud800',{'k':1},('one',),('one','two'))
        for name in ('style','color','highlight','size','font','underline','lang','url_id'):
            for value in values:
                def run(engine):
                    result=engine.RichText('initial')
                    try:result.add('text',**{name:value});return 'ok',result.xml
                    except Exception as error:return type(error).__name__,str(error),result.xml
                with self.subTest(name=name,value=repr(value)):self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_failed_conversion_keeps_exception_and_partial_state(self):
        for engine in (docxtpl,docxtpl_native):
            failure=ValueError('conversion');events=[]
            class Broken:
                def __str__(self):events.append('str');raise failure
            value=engine.RichText('before')
            before=value.xml
            with self.assertRaises(ValueError) as actual:value.add('text',size=Broken())
            self.assertIs(actual.exception,failure)
            self.assertEqual(value.xml,before)
            self.assertEqual(events,['str'])

    def test_byte_decode_result_is_not_coerced_again(self):
        def run(engine):
            events=[]
            class Bytes(bytes):
                def decode(self,*args,**kwargs):events.append((args,kwargs));return 42
            value=engine.RichText()
            try:value.add(Bytes(b'x'));return value.xml,events
            except Exception as error:return type(error).__name__,str(error),value.xml,events
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_constructor_dispatches_override_with_original_arguments(self):
        def run(engine):
            events=[]
            class Text:
                def __bool__(self):events.append('bool');return True
            token=Text();marker=object()
            class Rich(engine.RichText):
                def add(self,text,**props):events.append(('add',text is token,props['bold'] is marker,self.xml))
            result=Rich(token,bold=marker)
            return events,result.xml
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_paragraph_protocols_and_original_richtext_values(self):
        rich=object()  # stands for each engine's own RichText('original')
        values=('','<&>',3,b'\xffok','\ud800',rich)
        for value in values:
            for style in (None,'Style',('one',),('one','two')):
                def run(engine):
                    item=engine.RichText('original') if value is rich else value
                    result=engine.RichTextParagraph()
                    try:result.add(item,parastyle=style);return result.xml
                    except Exception as error:return type(error).__name__,str(error),result.xml
                with self.subTest(value=repr(value),style=style):self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_default_construction_does_not_execute_reference_methods(self):
        native=docxtpl_native._extension()
        with patch.object(docxtpl.RichText,'__init__',side_effect=AssertionError('reference constructor')), \
             patch.object(docxtpl.RichText,'add',side_effect=AssertionError('reference builder')), \
             patch.object(docxtpl.RichTextParagraph,'__init__',side_effect=AssertionError('reference paragraph constructor')), \
             patch.object(docxtpl.RichTextParagraph,'add',side_effect=AssertionError('reference paragraph builder')):
            for value in (3,b'\xffok','\ud800'):
                rich=docxtpl_native.RichText(value,size=-1)
                self.assertIn('<w:r>',rich.xml)
                paragraph=docxtpl_native.RichTextParagraph(value,parastyle='P')
                self.assertIn('<w:p>',paragraph.xml)

    def test_richtext_append_reads_destination_before_source(self):
        def run(engine):
            events=[]
            class Rich(engine.RichText):
                def __getattribute__(self,name):
                    if name=='xml':events.append(object.__getattribute__(self,'label'))
                    return super().__getattribute__(name)
            first=Rich();second=Rich()
            first.label='first';second.label='second'
            first.xml='A';second.xml='B'
            first.add(second)
            return first.xml,events
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_font_split_unpack_errors_and_partial_state(self):
        for split_result in ((),('x',),('x','y','z'),42):
            class Font:
                def __contains__(self,value):return True
                def split(self,*args):return split_result
            def run(engine):
                value=engine.RichText('before')
                try:value.add('text',font=Font());return 'ok',value.xml
                except Exception as error:return type(error).__name__,str(error),value.xml
            with self.subTest(split_result=split_result):self.assertEqual(run(docxtpl_native),run(docxtpl))


if __name__=='__main__':unittest.main()
