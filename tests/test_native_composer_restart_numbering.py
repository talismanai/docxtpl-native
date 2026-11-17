"""Differential numbering restart on real document styles."""
import importlib,unittest
from unittest.mock import patch
from docx import Document
from docx.oxml import parse_xml
from lxml import etree
import docxtpl,docxtpl_native
import docxcompose.composer as composer_module
W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
def observe(engine,style=None,disabled=False,local_id=None,twice=False):
    master=Document();child=Document();paragraph=master.add_paragraph('text',style=style);element=paragraph._p
    if local_id is not None:element.get_or_add_pPr().append(parse_xml(f'<w:numPr xmlns:w="{W}"><w:numId w:val="{local_id}"/></w:numPr>'))
    obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master);obj.restart_numbering=not disabled
    if local_id is not None:obj.num_id_mapping={'source':local_id}
    try:
        result=obj.restart_first_numbering(child,element)
        if twice:obj.restart_first_numbering(child,element)
        error=None
    except Exception as exc:result=None;error=(type(exc).__name__,str(exc))
    return result,error,etree.tostring(element),etree.tostring(master.part.numbering_part.element),obj.num_id_mapping,sorted(obj._numbering_restarted)

class RestartNumberingTests(unittest.TestCase):
    def test_real_styles_and_numbering(self):
        for style,disabled,local,twice in ((None,False,None,False),('Normal',False,None,False),('Heading 1',False,None,False),('List Bullet',False,None,False),('List Number',False,None,False),('List Number',False,1,False),('List Number',False,5,False),('List Number',False,None,True),('List Number',True,None,False)):
            with self.subTest(style=style,disabled=disabled,local=local,twice=twice):self.assertEqual(observe(docxtpl_native,style,disabled,local,twice),observe(docxtpl,style,disabled,local,twice))

    def test_xpath_and_deepcopy_callbacks_preserve_order(self):
        def run(engine):
            master=Document();child=Document();element=master.add_paragraph('text',style='List Number')._p;trace=[]
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            original_xpath=composer_module.xpath;original_copy=composer_module.deepcopy
            def xpath(value,query):trace.append(('xpath',query));return original_xpath(value,query)
            def copied(value):trace.append(('copy',etree.QName(value).localname));return original_copy(value)
            with patch.object(composer_module,'xpath',xpath),patch.object(composer_module,'deepcopy',copied):result=obj.restart_first_numbering(child,element)
            return result,trace,etree.tostring(element),etree.tostring(master.part.numbering_part.element)
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_numbering_insert_failure_preserves_partial_state(self):
        def run(engine):
            master=Document();child=Document();element=master.add_paragraph('text',style='List Number')._p;trace=[];marker=RuntimeError('insert failed')
            base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            class Composer(base):
                def _insert_num(self,value):trace.append(('insert',value.numId));raise marker
            obj=Composer(master)
            try:obj.restart_first_numbering(child,element)
            except Exception as error:return error is marker,trace,etree.tostring(element),etree.tostring(master.part.numbering_part.element),sorted(obj._numbering_restarted)
            raise AssertionError('insert failure was ignored')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_mapping_failure_follows_numbering_insertion(self):
        def run(engine):
            master=Document();child=Document();element=master.add_paragraph('text',style='List Number')._p;trace=[];marker=RuntimeError('mapping failed')
            element.get_or_add_pPr().append(parse_xml(f'<w:numPr xmlns:w="{W}"><w:numId w:val="5"/></w:numPr>'))
            base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            class Composer(base):
                def _replace_mapped_num_id(self,old,new):trace.append((old,new));raise marker
            obj=Composer(master)
            try:obj.restart_first_numbering(child,element)
            except Exception as error:return error is marker,trace,etree.tostring(element),etree.tostring(master.part.numbering_part.element),sorted(obj._numbering_restarted)
            raise AssertionError('mapping failure was ignored')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_custom_next_ids_iterator(self):
        def run(engine):
            master=Document();child=Document();element=master.add_paragraph('text',style='List Number')._p;trace=[]
            base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            class Composer(base):
                def _next_numbering_ids(self):
                    def values():trace.append('num');yield 10;trace.append('abstract');yield 9
                    return values()
            obj=Composer(master);result=obj.restart_first_numbering(child,element)
            return result,trace,etree.tostring(element),etree.tostring(master.part.numbering_part.element)
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_missing_abstract_id_preserves_failure(self):
        def run(engine):
            master=Document();child=Document();element=master.add_paragraph('text',style='List Number')._p
            root=master.part.numbering_part.element
            number=root.xpath('.//w:num[@w:numId="5"]')[0]
            number.remove(number.xpath('./w:abstractNumId')[0])
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            try:obj.restart_first_numbering(child,element)
            except Exception as error:return type(error).__name__,str(error),etree.tostring(element),etree.tostring(root),sorted(obj._numbering_restarted)
            raise AssertionError('missing abstract id was accepted')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_custom_restarted_set_protocol(self):
        def run(engine):
            master=Document();child=Document();element=master.add_paragraph('text',style='List Number')._p;trace=[]
            class Restarted:
                def __contains__(self,value):trace.append(('contains',value));return False
                def add(self,value):trace.append(('add',value))
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master);obj._numbering_restarted=Restarted()
            result=obj.restart_first_numbering(child,element)
            return result,trace,etree.tostring(element),etree.tostring(master.part.numbering_part.element)
        self.assertEqual(run(docxtpl_native),run(docxtpl))

if __name__=='__main__':unittest.main()
