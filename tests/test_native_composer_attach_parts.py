"""Differential orchestration and real document graphs for attach_parts."""
import importlib,io,unittest
from unittest.mock import patch
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import parse_xml
from docxcompose.properties import CustomProperties
from lxml import etree
from PIL import Image
import docxtpl,docxtpl_native
W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'

def observe(engine,case):
    master=Document();master.add_paragraph('parent');child=Document();paragraph=child.add_paragraph('child')
    if case=='style':
        style=child.styles.add_style('New Example',WD_STYLE_TYPE.PARAGRAPH);style.font.bold=True;paragraph.style=style
    if case=='number':paragraph.style='List Number'
    if case=='image':
        stream=io.BytesIO();Image.new('RGB',(3,2),(30,70,110)).save(stream,format='PNG');stream.seek(0);paragraph.add_run().add_picture(stream)
    if case=='footnote':
        part=Part(PackURI('/word/footnotes.xml'),'application/xml',f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="7"><w:p><w:r><w:t>Note</w:t></w:r></w:p></w:footnote></w:footnotes>'.encode(),child.part.package)
        child.part.relate_to(part,RT.FOOTNOTES);paragraph.add_run()._r.append(parse_xml(f'<w:footnoteReference xmlns:w="{W}" w:id="7"/>'))
    if case in ('property','property_field','property_keep'):
        CustomProperties(child)['Example']='Value'
    if case in ('property_field','property_keep'):
        paragraph._p.append(parse_xml(f'<w:fldSimple xmlns:w="{W}" w:instr=" DOCPROPERTY Example \\* MERGEFORMAT "><w:r><w:t>Value</w:t></w:r></w:fldSimple>'))
    module=importlib.import_module(engine.__name__+'.subdoc');obj=module.SubdocComposer(master)
    try:result=obj.attach_parts(child,remove_property_fields=case!='property_keep');error=None
    except Exception as exc:result=None;error=(type(exc).__name__,str(exc))
    relationships=[]
    for rel in master.part.rels.values():
        if rel.is_external:relationships.append((rel.rId,rel.reltype,rel.target_ref,None))
        else:relationships.append((rel.rId,rel.reltype,str(rel.target_part.partname),rel.target_part.blob))
    footnotes=next((r.target_part.blob for r in master.part.rels.values() if r.reltype==RT.FOOTNOTES),None)
    return result,error,etree.tostring(child.element),etree.tostring(master.styles.element),etree.tostring(master.part.numbering_part.element),relationships,footnotes

class AttachPartsTests(unittest.TestCase):
    def test_real_document_graphs(self):
        for case in ('plain','style','number','image','footnote','property','property_field','property_keep'):
            with self.subTest(case=case):self.assertEqual(observe(docxtpl_native,case),observe(docxtpl,case))

    def test_property_field_dissolves_only_when_enabled(self):
        dissolved=observe(docxtpl_native,'property_field')[2]
        retained=observe(docxtpl_native,'property_keep')[2]
        self.assertNotIn(b'fldSimple',dissolved)
        self.assertIn(b'fldSimple',retained)
        self.assertIn(b'Value',dissolved)

    def test_callbacks_and_property_order(self):
        def run(engine,remove):
            module=importlib.import_module(engine.__name__+'.subdoc');master=Document();child=Document();child.add_paragraph('one');child.add_paragraph('two');trace=[]
            class Properties:
                def __init__(self,doc):trace.append(('properties',doc is child))
                def keys(self):trace.append('keys');return iter(('a','b'))
                def dissolve_fields(self,name):trace.append(('dissolve',name))
            class Composer(module.SubdocComposer):
                def reset_reference_mapping(self):trace.append('reset')
                def _create_style_id_mapping(self,doc):trace.append(('map',doc is child))
                def add_referenced_parts(self,source,target,element):trace.append(('references',source is child.part,target is master.part,element.tag))
                def add_styles(self,doc,element):trace.append(('styles',doc is child,element.tag))
                def add_numberings(self,doc,element):trace.append(('numberings',doc is child,element.tag))
                def restart_first_numbering(self,doc,element):trace.append(('restart',doc is child,element.tag))
                def add_images(self,doc,element):trace.append(('images',doc is child,element.tag))
                def add_diagrams(self,doc,element):trace.append(('diagrams',doc is child,element.tag))
                def add_shapes(self,doc,element):trace.append(('shapes',doc is child,element.tag))
                def add_footnotes(self,doc,element):trace.append(('footnotes',doc is child,element.tag))
                def remove_header_and_footer_references(self,doc,element):trace.append(('headers',doc is child,element.tag))
                def add_styles_from_other_parts(self,doc):trace.append(('other_styles',doc is child))
                def renumber_bookmarks(self):trace.append('bookmarks')
                def renumber_docpr_ids(self):trace.append('docpr')
                def renumber_nvpicpr_ids(self):trace.append('nvpicpr')
                def fix_section_types(self,doc):trace.append(('sections',doc is child))
            obj=Composer(master);trace.clear()
            with patch.object(module,'CustomProperties',Properties):result=obj.attach_parts(child,remove_property_fields=remove)
            return result,trace
        for remove in (True,False):
            with self.subTest(remove=remove):self.assertEqual(run(docxtpl_native,remove),run(docxtpl,remove))

    def test_callback_failure_stops_following_steps(self):
        def run(engine):
            module=importlib.import_module(engine.__name__+'.subdoc');master=Document();child=Document();child.add_paragraph('one');trace=[];marker=RuntimeError('numbering failure')
            class Composer(module.SubdocComposer):
                def _create_style_id_mapping(self,doc):trace.append('map')
                def add_referenced_parts(self,*args):trace.append('references')
                def add_styles(self,*args):trace.append('styles')
                def add_numberings(self,*args):trace.append('numberings');raise marker
                def restart_first_numbering(self,*args):trace.append('restart')
            obj=Composer(master)
            try:obj.attach_parts(child,remove_property_fields=False)
            except Exception as error:return error is marker,trace
            raise AssertionError('callback failure was ignored')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_custom_isinstance_and_section_type_are_resolved(self):
        import builtins
        def run(engine,replace_section):
            module=importlib.import_module(engine.__name__+'.subdoc');master=Document();child=Document();child.add_paragraph('one');trace=[]
            class NoSection:pass
            section=NoSection if replace_section else module.CT_SectPr
            class Composer(module.SubdocComposer):
                def _create_style_id_mapping(self,doc):pass
                def add_referenced_parts(self,*args):trace.append(('references',args[2].tag))
                def add_styles(self,*args):pass
                def add_numberings(self,*args):pass
                def restart_first_numbering(self,*args):pass
                def add_images(self,*args):pass
                def add_diagrams(self,*args):pass
                def add_shapes(self,*args):pass
                def add_footnotes(self,*args):pass
                def remove_header_and_footer_references(self,*args):pass
                def add_styles_from_other_parts(self,*args):pass
                def renumber_bookmarks(self):pass
                def renumber_docpr_ids(self):pass
                def renumber_nvpicpr_ids(self):pass
                def fix_section_types(self,*args):pass
            def check(value,kind):trace.append(('isinstance',value.tag,kind is section));return builtins.isinstance(value,kind)
            obj=Composer(master)
            with patch.object(module,'CT_SectPr',section),patch.object(module,'isinstance',check,create=True):
                result=obj.attach_parts(child,remove_property_fields=False)
            return result,trace
        for replace in (False,True):
            with self.subTest(replace=replace):self.assertEqual(run(docxtpl_native,replace),run(docxtpl,replace))

    def test_remove_property_flag_truthiness(self):
        def run(engine):
            module=importlib.import_module(engine.__name__+'.subdoc');master=Document();child=Document();trace=[]
            class Flag:
                def __bool__(self):trace.append('bool');return False
            class Properties:
                def __init__(self,doc):raise AssertionError('must not be created')
            class Composer(module.SubdocComposer):
                def _create_style_id_mapping(self,doc):trace.append('map')
                def add_styles_from_other_parts(self,doc):pass
                def renumber_bookmarks(self):pass
                def renumber_docpr_ids(self):pass
                def renumber_nvpicpr_ids(self):pass
                def fix_section_types(self,doc):pass
            obj=Composer(master)
            with patch.object(module,'CustomProperties',Properties):result=obj.attach_parts(child,Flag())
            return result,trace
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_property_constructor_failure_follows_reset(self):
        def run(engine):
            module=importlib.import_module(engine.__name__+'.subdoc');trace=[];marker=RuntimeError('properties failed')
            class Properties:
                def __init__(self,doc):trace.append('properties');raise marker
            class Composer(module.SubdocComposer):
                def reset_reference_mapping(self):trace.append('reset')
                def _create_style_id_mapping(self,doc):trace.append('map')
            obj=Composer(Document());trace.clear()
            try:
                with patch.object(module,'CustomProperties',Properties):obj.attach_parts(Document())
            except Exception as error:return error is marker,trace
            raise AssertionError('property error was ignored')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_property_failure_keeps_previous_dissolutions(self):
        def run(engine):
            module=importlib.import_module(engine.__name__+'.subdoc');trace=[];marker=RuntimeError('property failed')
            class Properties:
                def __init__(self,doc):trace.append('properties')
                def keys(self):trace.append('keys');return iter(('a','b'))
                def dissolve_fields(self,name):
                    trace.append(('dissolve',name))
                    if name=='b':raise marker
            class Composer(module.SubdocComposer):
                def reset_reference_mapping(self):trace.append('reset')
                def _create_style_id_mapping(self,doc):trace.append('map')
            obj=Composer(Document());trace.clear()
            try:
                with patch.object(module,'CustomProperties',Properties):obj.attach_parts(Document())
            except Exception as error:return error is marker,trace
            raise AssertionError('property failure was ignored')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_reimported_module_uses_its_own_globals(self):
        import builtins,importlib.util,sys
        def run(engine):
            original=importlib.import_module(engine.__name__+'.subdoc');name=engine.__name__+'._attach_alias'
            spec=importlib.util.spec_from_file_location(name,original.__file__);module=importlib.util.module_from_spec(spec)
            sys.modules[name]=module;spec.loader.exec_module(module);trace=[]
            class Properties:
                def __init__(self,doc):trace.append('properties')
                def keys(self):trace.append('keys');return []
            class Composer(module.SubdocComposer):
                def _create_style_id_mapping(self,doc):trace.append('map')
                def add_styles_from_other_parts(self,doc):pass
                def renumber_bookmarks(self):pass
                def renumber_docpr_ids(self):pass
                def renumber_nvpicpr_ids(self):pass
                def fix_section_types(self,doc):pass
            def check(value,kind):trace.append(('isinstance',kind is module.CT_SectPr));return builtins.isinstance(value,kind)
            try:
                module.CustomProperties=Properties;module.isinstance=check
                result=Composer(Document()).attach_parts(Document())
                return result,trace
            finally:del sys.modules[name]
        self.assertEqual(run(docxtpl_native),run(docxtpl))

if __name__=='__main__':unittest.main()
