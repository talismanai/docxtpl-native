"""Style copying and remapping on real document graphs."""
import importlib,re,unittest
from unittest.mock import patch
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import parse_xml
from docx.oxml.ns import qn
from lxml import etree
import docxcompose.composer as composer_module
import docxtpl,docxtpl_native

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
# Copied list nsids: random in docxcompose, derived from the definition in
# docxtpl-native (intentional divergence; see test_native_composer_add_numberings).
NSID=re.compile(rb'(<w:nsid w:val=")[0-9A-Fa-f]{8}(")')
def observe(engine,case):
    master=Document();child=Document();base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
    if case in ('existing','existing_numbered'):
        master_style=master.styles.add_style('Shared Example',WD_STYLE_TYPE.PARAGRAPH)
        master_style.element.set(qn('w:styleId'),'MasterId')
        child_style=child.styles.add_style('Shared Example',WD_STYLE_TYPE.PARAGRAPH)
        child_style.element.set(qn('w:styleId'),'ChildId')
    else:
        child_style=child.styles.add_style('New Example',WD_STYLE_TYPE.PARAGRAPH)
    if case=='linked':
        linked=child.styles.add_style('Linked Example',WD_STYLE_TYPE.CHARACTER)
        child_style.element.append(parse_xml(f'<w:link xmlns:w="{W}" w:val="{linked.style_id}"/>'))
    if case in ('numbered','existing_numbered'):
        child_style.element.append(parse_xml(f'<w:pPr xmlns:w="{W}"><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr>'))
        if case=='existing_numbered':
            master_style.element.append(parse_xml(f'<w:pPr xmlns:w="{W}"><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr>'))
    paragraph=child.add_paragraph('text');paragraph.style=child_style
    element=paragraph._p
    if case=='missing':element.pPr.pStyle.val='NoSuchStyle'
    obj=base(master);obj._create_style_id_mapping(child)
    try:
        with patch.object(composer_module.random,'random',lambda:0.12345678):result=obj.add_styles(child,element)
        error=None
    except Exception as exc:result=None;error=(type(exc).__name__,str(exc))
    return result,error,etree.tostring(element),etree.tostring(master.styles.element),NSID.sub(rb'\g<1>--------\g<2>',etree.tostring(master.part.numbering_part.element)),obj.num_id_mapping,obj.anum_id_mapping

class AddStylesTests(unittest.TestCase):
    def test_real_styles(self):
        for case in ('new','existing','existing_numbered','linked','numbered','missing'):
            with self.subTest(case=case):self.assertEqual(observe(docxtpl_native,case),observe(docxtpl,case))

    def test_numbering_callback_failure_keeps_appended_style(self):
        def run(engine):
            master=Document();child=Document();style=child.styles.add_style('New Example',WD_STYLE_TYPE.PARAGRAPH)
            paragraph=child.add_paragraph('text');paragraph.style=style;marker=RuntimeError('numbering failed');trace=[]
            base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            class Composer(base):
                def add_numberings(self,doc,element):trace.append((doc is child,element.getparent() is master.styles.element));raise marker
            obj=Composer(master);obj._create_style_id_mapping(child)
            try:obj.add_styles(child,paragraph._p)
            except Exception as error:return error is marker,trace,etree.tostring(master.styles.element),etree.tostring(paragraph._p)
            raise AssertionError('callback error was ignored')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_deepcopy_failure_leaves_destination_unchanged(self):
        def run(engine):
            master=Document();child=Document();style=child.styles.add_style('New Example',WD_STYLE_TYPE.PARAGRAPH)
            paragraph=child.add_paragraph('text');paragraph.style=style;marker=RuntimeError('copy failed')
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master);obj._create_style_id_mapping(child)
            before=etree.tostring(master.styles.element)
            def copy(value):raise marker
            try:
                with patch.object(composer_module,'deepcopy',copy):obj.add_styles(child,paragraph._p)
            except Exception as error:return error is marker,before==etree.tostring(master.styles.element),etree.tostring(paragraph._p)
            raise AssertionError('copy error was ignored')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_duplicate_style_references_are_processed_once(self):
        def run(engine):
            master=Document();child=Document();style=child.styles.add_style('New Example',WD_STYLE_TYPE.PARAGRAPH)
            paragraph=child.add_paragraph('text');paragraph.style=style
            paragraph._p.get_or_add_pPr().append(parse_xml(f'<w:pStyle xmlns:w="{W}" w:val="{style.style_id}"/>'))
            trace=[];base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            class Composer(base):
                def mapped_style_id(self,value):trace.append(value);return super().mapped_style_id(value)
            obj=Composer(master);obj._create_style_id_mapping(child);result=obj.add_styles(child,paragraph._p)
            return result,trace,etree.tostring(master.styles.element),etree.tostring(paragraph._p)
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_user_xpath_and_list_constructor_are_used(self):
        def run(engine):
            master=Document();child=Document();style=child.styles.add_style('New Example',WD_STYLE_TYPE.PARAGRAPH)
            paragraph=child.add_paragraph('text');paragraph.style=style;trace=[]
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master);obj._create_style_id_mapping(child)
            original_xpath=composer_module.xpath
            def xpath(element,query):trace.append(('xpath',query));return original_xpath(element,query)
            def as_list(value):trace.append(('list',type(value).__name__));return list(value)
            with patch.object(composer_module,'xpath',xpath),patch.object(composer_module,'list',as_list,create=True):
                result=obj.add_styles(child,paragraph._p)
            return result,trace,etree.tostring(master.styles.element),etree.tostring(paragraph._p)
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_unhashable_style_id_error(self):
        from types import SimpleNamespace
        def run(engine):
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(Document())
            try:
                with patch.object(composer_module,'xpath',lambda *args:[SimpleNamespace(val=[])]):
                    obj.add_styles(Document(),object())
            except Exception as error:return type(error).__name__,str(error)
            raise AssertionError('unhashable style identifier was accepted')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_existing_numbering_uses_custom_integer_conversion(self):
        def run(engine):
            master=Document();child=Document();trace=[]
            master_style=master.styles.add_style('Shared Example',WD_STYLE_TYPE.PARAGRAPH)
            child_style=child.styles.add_style('Shared Example',WD_STYLE_TYPE.PARAGRAPH)
            for style in (master_style,child_style):
                style.element.append(parse_xml(f'<w:pPr xmlns:w="{W}"><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr>'))
            paragraph=child.add_paragraph('text');paragraph.style=child_style
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master);obj._create_style_id_mapping(child)
            def integer(value):trace.append(value);return int(value)
            with patch.object(composer_module,'int',integer,create=True):obj.add_styles(child,paragraph._p)
            return trace,obj.anum_id_mapping,etree.tostring(paragraph._p)
        self.assertEqual(run(docxtpl_native),run(docxtpl))

if __name__=='__main__':unittest.main()
