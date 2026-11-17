"""Differential numbering copy on real document graphs."""
import importlib,re,unittest
from unittest.mock import patch
from docx import Document
from docx.oxml import parse_xml
from lxml import etree
import docxcompose.composer as composer_module
import docxtpl,docxtpl_native

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
NSID=re.compile(rb'(<w:nsid w:val=")[0-9A-Fa-f]{8}(")')
def neutral(xml):
    """Numbering XML without nsid values: docxcompose draws them from Python's
    random, docxtpl-native derives them from the definition (intentional
    divergence, see test_nsid_is_derived_without_python_random)."""
    return NSID.sub(rb'\g<1>--------\g<2>',xml)
def observe(engine,ids,preexisting=None):
    parent=Document();child=Document();base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
    instance=base(parent)
    if preexisting:instance.num_id_mapping.update(preexisting)
    refs=''.join(f'<w:numPr><w:numId w:val="{value}"/></w:numPr>' for value in ids)
    element=parse_xml(f'<w:p xmlns:w="{W}"><w:pPr>{refs}</w:pPr></w:p>')
    with patch.object(composer_module.random,'random',lambda:0.12345678):
        try:result=instance.add_numberings(child,element);error=None
        except Exception as exc:result=None;error=(type(exc).__name__,str(exc))
    return result,error,etree.tostring(element),neutral(etree.tostring(parent.part.numbering_part.element)),instance.num_id_mapping,instance.anum_id_mapping

class AddNumberingsTests(unittest.TestCase):
    def test_real_copy_missing_and_existing_mapping(self):
        for ids,mapping in (([],None),([1],None),([1,2],None),([1,1],None),([999],None),([1],{1:42})):
            with self.subTest(ids=ids,mapping=mapping):self.assertEqual(observe(docxtpl_native,ids,mapping),observe(docxtpl,ids,mapping))
    def test_no_ids_does_not_resolve_next_id(self):
        def run(engine):
            base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            class Composer(base):
                def _next_numbering_ids(self):raise AssertionError('not needed')
            return Composer(Document()).add_numberings(Document(),parse_xml(f'<w:p xmlns:w="{W}"/>'))
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_missing_abstract_preserves_partial_mapping(self):
        def run(engine):
            parent=Document();child=Document();root=child.part.numbering_part.element
            num=root.xpath('.//w:num[@w:numId="1"]')[0]
            abstract_id=num.xpath('./w:abstractNumId')[0].val
            abstract=root.xpath(f'.//w:abstractNum[@w:abstractNumId="{abstract_id}"]')[0]
            root.remove(abstract)
            element=parse_xml(f'<w:p xmlns:w="{W}"><w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:p>')
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(parent)
            result=obj.add_numberings(child,element)
            return result,etree.tostring(element),obj.num_id_mapping,obj.anum_id_mapping,etree.tostring(parent.part.numbering_part.element)
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_deepcopy_failure_keeps_partial_mapping_and_exception(self):
        def run(engine):
            parent=Document();child=Document();marker=RuntimeError('copy failed');trace=[]
            element=parse_xml(f'<w:p xmlns:w="{W}"><w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:p>')
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(parent)
            original=composer_module.deepcopy
            def copied(value):
                trace.append(etree.QName(value).localname)
                if len(trace)==2:raise marker
                return original(value)
            try:
                with patch.object(composer_module,'deepcopy',copied):obj.add_numberings(child,element)
            except Exception as error:
                return error is marker,trace,obj.num_id_mapping,obj.anum_id_mapping,etree.tostring(element),etree.tostring(parent.part.numbering_part.element)
            raise AssertionError('copy error was ignored')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_next_ids_may_be_supplied_by_generator(self):
        def run(engine):
            parent=Document();child=Document();trace=[]
            base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            class Composer(base):
                def _next_numbering_ids(self):
                    def values():trace.append('first');yield 10;trace.append('second');yield 9
                    return values()
            obj=Composer(parent)
            element=parse_xml(f'<w:p xmlns:w="{W}"><w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:p>')
            with patch.object(composer_module.random,'random',lambda:0.12345678):obj.add_numberings(child,element)
            return trace,etree.tostring(element),obj.num_id_mapping,obj.anum_id_mapping,neutral(etree.tostring(parent.part.numbering_part.element))
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_nsid_is_derived_without_python_random(self):
        """docxcompose draws the copied list's nsid from Python's random (a
        failing draw aborts the copy); docxtpl-native derives it from the
        definition, never calls random and gives the same nsid every time."""
        def run():
            parent=Document();child=Document()
            element=parse_xml(f'<w:p xmlns:w="{W}"><w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:p>')
            obj=importlib.import_module('docxtpl_native.subdoc').SubdocComposer(parent)
            def random():raise AssertionError('random drawn')
            with patch.object(composer_module.random,'random',random):obj.add_numberings(child,element)
            return etree.tostring(parent.part.numbering_part.element)
        first=run()
        self.assertEqual(first,run())
        added=re.findall(rb'<w:nsid w:val="([0-9A-F]{8})"',first)
        self.assertEqual(len(added),len(set(added)))

    def test_custom_set_constructor_is_used(self):
        def run(engine):
            parent=Document();child=Document();trace=[]
            element=parse_xml(f'<w:p xmlns:w="{W}"><w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:p>')
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(parent)
            def selected(values):trace.append(list(values));return list(values)
            with patch.object(composer_module,'set',selected,create=True),patch.object(composer_module.random,'random',lambda:0.12345678):
                result=obj.add_numberings(child,element)
            return result,trace,obj.num_id_mapping,obj.anum_id_mapping,etree.tostring(element),neutral(etree.tostring(parent.part.numbering_part.element))
        self.assertEqual(run(docxtpl_native),run(docxtpl))

if __name__=='__main__':unittest.main()
