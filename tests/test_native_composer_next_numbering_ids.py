"""Next numbering identifiers compared to the reference."""
import importlib,unittest
from types import SimpleNamespace
from unittest.mock import patch
from docx import Document
from docx.oxml import parse_xml
import docxcompose.composer as composer_module
import docxtpl,docxtpl_native

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
def run(engine,source,override=False):
    root=parse_xml(f'<w:numbering xmlns:w="{W}">{source}</w:numbering>');trace=[]
    base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
    class Composer(base):
        def numbering_part(self):trace.append('part');return SimpleNamespace(element=root)
    composer=Composer(Document())
    def maximum(items):trace.append(('max',list(items)));return max(items)
    def integer(value):trace.append(('int',value));return int(value)
    try:
        if override:
            with patch.object(composer_module,'max',maximum,create=True),patch.object(composer_module,'int',integer,create=True):result=composer._next_numbering_ids()
        else:result=composer._next_numbering_ids()
        error=None
    except Exception as exc:result=None;error=(type(exc).__name__,str(exc))
    return result,error,trace

class NextNumberingIdsTests(unittest.TestCase):
    def test_empty_and_real_numbering(self):
        for source in ('','<w:abstractNum w:abstractNumId="0"/><w:abstractNum w:abstractNumId="8"/><w:num w:numId="2"/><w:num w:numId="10"/>'):
            for override in (False,True):
                with self.subTest(source=source,override=override):self.assertEqual(run(docxtpl_native,source,override),run(docxtpl,source,override))
    def test_invalid_number(self):
        for source in ('<w:abstractNum w:abstractNumId="bad"/>','<w:num w:numId="bad"/>'):
            with self.subTest(source=source):self.assertEqual(run(docxtpl_native,source),run(docxtpl,source))

    def test_custom_maximum_addition_preserves_identity(self):
        def execute(engine):
            root=parse_xml(f'<w:numbering xmlns:w="{W}"><w:abstractNum w:abstractNumId="2"/><w:num w:numId="3"/></w:numbering>')
            base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer;trace=[];results=[object(),object()]
            class Composer(base):
                def numbering_part(self):return SimpleNamespace(element=root)
            class Maximum:
                def __init__(self,index):self.index=index
                def __add__(self,value):trace.append(('add',self.index,value));return results[self.index]
            def maximum(values):
                trace.append(('max',list(values)));return Maximum(sum(event[0]=='max' for event in trace)-1)
            with patch.object(composer_module,'max',maximum,create=True):value=Composer(Document())._next_numbering_ids()
            return trace,value[0] is results[0],value[1] is results[1]
        self.assertEqual(execute(docxtpl_native),execute(docxtpl))

    def test_xpath_is_resolved_again_after_max_callback(self):
        def execute(engine):
            base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer;trace=[]
            class Composer(base):
                def numbering_part(self):return SimpleNamespace(element=object())
            def first(element,query):trace.append(('first',query));return [SimpleNamespace(numId=3)]
            def second(element,query):trace.append(('second',query));return ['27']
            def maximum(values):trace.append(('max',list(values)));composer_module.xpath=second;return max(values)
            with patch.object(composer_module,'xpath',first),patch.object(composer_module,'max',maximum,create=True):
                result=Composer(Document())._next_numbering_ids()
            return result,trace
        self.assertEqual(execute(docxtpl_native),execute(docxtpl))

if __name__=='__main__':unittest.main()
