"""Differential footnote transfer using real parts and references."""
import importlib,unittest
from unittest.mock import patch
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import parse_xml
from lxml import etree
import docxcompose.composer as composer_module
import docxtpl,docxtpl_native
W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'

def observe(engine,ids,missing=False):
    parent=Document();child=Document()
    part=Part(PackURI('/word/footnotes.xml'),'application/xml',f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="7"><w:p><w:r><w:t>First note</w:t></w:r></w:p></w:footnote><w:footnote w:id="8"><w:p><w:r><w:t>Second note</w:t></w:r></w:p></w:footnote></w:footnotes>'.encode(),child.part.package)
    if not missing:child.part.relate_to(part,RT.FOOTNOTES)
    refs=''.join(f'<w:r><w:footnoteReference w:id="{value}"/></w:r>' for value in ids)
    element=parse_xml(f'<w:p xmlns:w="{W}">{refs}</w:p>')
    composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(parent)
    try:result=composer.add_footnotes(child,element);error=None
    except Exception as exc:result=None;error=(type(exc).__name__,str(exc))
    rel=next((r for r in parent.part.rels.values() if r.reltype==RT.FOOTNOTES),None)
    return result,error,etree.tostring(element),None if rel is None else rel.target_part.blob

class AddFootnotesTests(unittest.TestCase):
    def test_empty_single_multiple_missing(self):
        for ids,missing in (([],False),([7],False),([7,8],False),([99],False),([7,99],False),([7],True)):
            with self.subTest(ids=ids,missing=missing):self.assertEqual(observe(docxtpl_native,ids,missing),observe(docxtpl,ids,missing))

    def test_no_references_do_not_read_parts(self):
        def run(engine):
            base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            class Composer(base):
                def footnote_part(self):raise AssertionError('unnecessary')
            return Composer(Document()).add_footnotes(Document(),parse_xml(f'<w:p xmlns:w="{W}"/>'))
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_existing_destination_uses_count_for_next_id(self):
        def run(engine):
            parent=Document();child=Document()
            target=Part(PackURI('/word/footnotes.xml'),'application/xml',f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="50"/></w:footnotes>'.encode(),parent.part.package)
            source=Part(PackURI('/word/footnotes.xml'),'application/xml',f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="7"><w:p/></w:footnote></w:footnotes>'.encode(),child.part.package)
            parent.part.relate_to(target,RT.FOOTNOTES);child.part.relate_to(source,RT.FOOTNOTES)
            element=parse_xml(f'<w:p xmlns:w="{W}"><w:r><w:footnoteReference w:id="7"/></w:r></w:p>')
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(parent)
            result=obj.add_footnotes(child,element)
            return result,etree.tostring(element),target.blob
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_copy_failure_preserves_partial_changes(self):
        def run(engine):
            parent=Document();child=Document();trace=[];marker=RuntimeError('copy failed')
            source=Part(PackURI('/word/footnotes.xml'),'application/xml',f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="7"/><w:footnote w:id="8"/></w:footnotes>'.encode(),child.part.package)
            child.part.relate_to(source,RT.FOOTNOTES)
            element=parse_xml(f'<w:p xmlns:w="{W}"><w:r><w:footnoteReference w:id="7"/><w:footnoteReference w:id="8"/></w:r></w:p>')
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(parent);original=composer_module.deepcopy
            def copied(value):
                trace.append(etree.QName(value).localname)
                if len(trace)==2:raise marker
                return original(value)
            try:
                with patch.object(composer_module,'deepcopy',copied):obj.add_footnotes(child,element)
            except Exception as error:
                return error is marker,trace,etree.tostring(element),obj.footnote_part().blob
            raise AssertionError('copy failure was ignored')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_referenced_parts_failure_keeps_partial_changes(self):
        def run(engine):
            parent=Document();child=Document();marker=RuntimeError('relationships failed');trace=[]
            source=Part(PackURI('/word/footnotes.xml'),'application/xml',f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="7"/></w:footnotes>'.encode(),child.part.package)
            child.part.relate_to(source,RT.FOOTNOTES)
            element=parse_xml(f'<w:p xmlns:w="{W}"><w:r><w:footnoteReference w:id="7"/></w:r></w:p>')
            base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            class Composer(base):
                def add_referenced_parts(self,src,dst,root):trace.append((src is source,dst is self.footnote_part(),etree.QName(root).localname));raise marker
            obj=Composer(parent)
            try:obj.add_footnotes(child,element)
            except Exception as error:return error is marker,trace,etree.tostring(element),obj.footnote_part().blob
            raise AssertionError('relationship failure was ignored')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_custom_length_addition_and_string_conversion(self):
        def run(engine):
            parent=Document();child=Document();trace=[]
            source=Part(PackURI('/word/footnotes.xml'),'application/xml',f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="7"/></w:footnotes>'.encode(),child.part.package)
            child.part.relate_to(source,RT.FOOTNOTES)
            element=parse_xml(f'<w:p xmlns:w="{W}"><w:r><w:footnoteReference w:id="7"/></w:r></w:p>')
            class Counter:
                def __add__(self,value):trace.append(('add',value));return self
                def __iadd__(self,value):trace.append(('iadd',value));return self
                def __str__(self):trace.append('str');return '42'
            def length(value):trace.append('len');return Counter()
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(parent)
            with patch.object(composer_module,'len',length,create=True):result=obj.add_footnotes(child,element)
            return result,trace,etree.tostring(element),obj.footnote_part().blob
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_external_relationships_match_reference_graph(self):
        def run(engine):
            parent=Document();child=Document();r='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
            target=Part(PackURI('/word/footnotes.xml'),'application/xml',f'<w:footnotes xmlns:w="{W}"/>'.encode(),parent.part.package)
            target.relate_to('https://existing.invalid',RT.HYPERLINK,is_external=True)
            source=Part(PackURI('/word/footnotes.xml'),'application/xml',f'<w:footnotes xmlns:w="{W}" xmlns:r="{r}"><w:footnote w:id="7"><w:p><w:hyperlink r:id="rId1"/></w:p></w:footnote></w:footnotes>'.encode(),child.part.package)
            source.relate_to('https://source.invalid',RT.HYPERLINK,is_external=True)
            parent.part.relate_to(target,RT.FOOTNOTES);child.part.relate_to(source,RT.FOOTNOTES)
            element=parse_xml(f'<w:p xmlns:w="{W}"><w:r><w:footnoteReference w:id="7"/></w:r></w:p>')
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(parent)
            result=obj.add_footnotes(child,element)
            relationships=[(rel.rId,rel.reltype,rel.target_ref,rel.is_external) for rel in target.rels.values()]
            return result,etree.tostring(element),target.blob,relationships
        self.assertEqual(run(docxtpl_native),run(docxtpl))

if __name__=='__main__':unittest.main()
