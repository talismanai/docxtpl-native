"""Bookmark renumbering on exposed composer trees."""
import importlib
import unittest

from docx import Document
from docx.oxml import parse_xml
import docxtpl
import docxtpl_native

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'


class NativeComposerBookmarkTests(unittest.TestCase):
    def test_real_bookmarks_are_renumbered_independently(self):
        def run(engine):
            document=Document();paragraph=document.add_paragraph()._p
            for kind,number in [('Start',8),('End',7),('Start',3),('End',8),('Start',4)]:
                paragraph.append(parse_xml(f'<w:bookmark{kind} xmlns:w="{W}" w:id="{number}"/>'))
            composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(document)
            result=composer.renumber_bookmarks()
            return result,[(node.tag.rsplit('}',1)[1],node.get(f'{{{W}}}id')) for node in paragraph]
        actual=run(docxtpl_native)
        self.assertEqual(actual,run(docxtpl))
        self.assertEqual([value for _,value in actual[1]],['0','0','1','1','2'])

    def test_dynamic_xpath_namespace_and_partial_failure(self):
        import docxcompose.composer as module,sys
        def run(engine,fail):
            trace=[];marker=RuntimeError('bookmark set failed');nodes=[]
            class Node:
                def __init__(self,name):self.name=name;self.value=None;nodes.append(self)
                def set(self,name,value):
                    trace.append(('set',self.name,name,value))
                    if fail and self.name=='start1':raise marker
                    self.value=value
            starts=[Node('start0'),Node('start1')];ends=[Node('end0')]
            def callback(element,query):
                trace.append(('xpath',query))
                return iter(starts if query.endswith('Start') else ends)
            previous_xpath=module.xpath;previous_ns=module.NS
            try:
                module.xpath=callback;module.NS={'w':'urn:custom'}
                composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(Document())
                try:result=composer.renumber_bookmarks();error=None
                except Exception as exc:result=None;error=(type(exc).__name__,exc.args,exc is marker)
                return result,error,trace,[node.value for node in nodes],nodes[0].value is nodes[2].value,nodes[0].value is sys.intern('0')
            finally:module.xpath=previous_xpath;module.NS=previous_ns
        for fail in (False,True):
            with self.subTest(fail=fail):self.assertEqual(run(docxtpl_native,fail),run(docxtpl,fail))

    def test_rebound_str_is_called_and_result_identity_is_preserved(self):
        import docxcompose.composer as module
        def run(engine):
            trace=[];token=object()
            class Node:
                def set(self,name,value):trace.append(('set',name,value is token))
            def callback(element,query):return [Node()] if query.endswith('Start') else []
            def stringify(value):trace.append(('str',value));return token
            old_xpath=module.xpath;had_str=hasattr(module,'str');old_str=getattr(module,'str',None)
            try:
                module.xpath=callback;module.str=stringify
                importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(Document()).renumber_bookmarks()
                return trace
            finally:
                module.xpath=old_xpath
                if had_str:module.str=old_str
                else:del module.str
        self.assertEqual(run(docxtpl_native),run(docxtpl))


if __name__=='__main__': unittest.main()
