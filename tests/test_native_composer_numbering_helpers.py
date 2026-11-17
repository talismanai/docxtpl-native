"""Numbering helper behavior on real XML and user-supplied mappings."""
import importlib
import unittest
from types import SimpleNamespace
from docx import Document
from docx.oxml import parse_xml
from lxml import etree
import docxtpl
import docxtpl_native

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'

def insert(engine,method,has_num):
    source=f'<w:numbering xmlns:w="{W}"><w:abstractNum w:abstractNumId="5"/>'
    if has_num:source+='<w:num w:numId="2"/><w:num w:numId="3"/>'
    root=parse_xml(source+'</w:numbering>')
    tag='abstractNum' if method=='_insert_abstract_num' else 'num'
    inserted=parse_xml(f'<w:{tag} xmlns:w="{W}" w:{"abstractNumId" if tag=="abstractNum" else "numId"}="9"/>')
    base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
    class Composer(base):
        def numbering_part(self):return SimpleNamespace(element=root)
    result=getattr(Composer(Document()),method)(inserted)
    return result,etree.tostring(root),inserted.getparent() is root

class NativeNumberingHelpersTests(unittest.TestCase):
    def test_real_numbering_insertion_order(self):
        for method in ('_insert_num','_insert_abstract_num'):
            for has_num in (False,True):
                with self.subTest(method=method,has_num=has_num):
                    self.assertEqual(insert(docxtpl_native,method,has_num),insert(docxtpl,method,has_num))

    def test_replace_first_match_and_identity(self):
        def run(engine):
            cls=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            obj=cls(Document());old=object();new=object();other=object()
            obj.num_id_mapping={'first':old,'second':old,'third':other}
            result=obj._replace_mapped_num_id(old,new)
            return result,obj.num_id_mapping['first'] is new,obj.num_id_mapping['second'] is old,obj.num_id_mapping['third'] is other
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_equality_callback_can_replace_mapping(self):
        def run(engine):
            cls=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            obj=cls(Document());new=object();trace=[]
            class Value:
                def __eq__(self,other):
                    trace.append('eq');obj.num_id_mapping={'key':'old'};return True
            obj.num_id_mapping={'key':Value()}
            result=obj._replace_mapped_num_id(object(),new)
            return result,trace,obj.num_id_mapping['key'] is new
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_invalid_items_unpacking(self):
        def run(engine,pair):
            cls=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            obj=cls(Document())
            class Mapping:
                def items(self):return [pair]
            obj.num_id_mapping=Mapping()
            try:obj._replace_mapped_num_id(1,2)
            except Exception as error:return type(error).__name__,str(error)
            return None
        class BadIterator:
            def __iter__(self):raise TypeError('custom iterator error')
        for pair in ((),(1,),(1,2,3),1,None,BadIterator()):
            with self.subTest(pair=pair):self.assertEqual(run(docxtpl_native,pair),run(docxtpl,pair))

    def test_tuple_subclass_uses_its_iterator(self):
        def run(engine):
            trace=[];cls=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            obj=cls(Document())
            class Pair(tuple):
                def __iter__(self):trace.append('iter');return iter(('key',7))
            class Mapping:
                def items(self):return [Pair(('wrong',0))]
                def __setitem__(self,key,value):trace.append((key,value))
            obj.num_id_mapping=Mapping();obj._replace_mapped_num_id(7,8)
            return trace
        self.assertEqual(run(docxtpl_native),run(docxtpl))

if __name__=='__main__':unittest.main()
