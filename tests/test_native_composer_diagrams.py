"""Diagram relationship migration on exposed document graphs."""
import importlib
import unittest
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import parse_xml
from lxml import etree
import docxtpl
import docxtpl_native

R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
D='http://schemas.openxmlformats.org/drawingml/2006/diagram'
TYPES=(RT.DIAGRAM_DATA,RT.DIAGRAM_LAYOUT,RT.DIAGRAM_QUICK_STYLE,RT.DIAGRAM_COLORS)
KEYS=('dm','lo','qs','cs')

def observe(engine, missing=None, base=False):
    master=Document();child=Document();targets=[];ids=[]
    for index,kind in enumerate(TYPES):
        part=Part(PackURI(f'/word/diagrams/test{index}.xml'),'application/xml',f'<n>{index}</n>'.encode(),child.part.package)
        targets.append(part);ids.append(child.part.relate_to(part,kind))
    attrs=' '.join(f'r:{key}="{rid}"' for key,rid in zip(KEYS,ids) if key!=missing)
    source=f'<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:dgm="{D}" xmlns:r="{R}"><dgm:relIds {attrs}/></w:p>'
    element=parse_xml(source) if base else etree.fromstring(source)
    diagram=element[0]
    composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
    try: result=composer.add_diagrams(child,element);error=None
    except Exception as exc: result=None;error=(type(exc).__name__,exc.args)
    refs=[]
    for key in KEYS:
        rid=diagram.get(f'{{{R}}}{key}')
        rel=master.part.rels.get(rid)
        refs.append(None if rel is None else (rel.reltype,TYPES.index(rel.reltype),rel.target_part is targets[TYPES.index(rel.reltype)]))
    return result,error,refs,composer.doc is master,composer.pkg is master.part.package

class NativeComposerDiagramTests(unittest.TestCase):
    def test_real_diagrams_raw_and_docx_elements(self):
        for base in (False,True):
            with self.subTest(base=base):
                actual=observe(docxtpl_native,base=base);self.assertEqual(actual,observe(docxtpl,base=base))
                self.assertTrue(all(ref and ref[2] for ref in actual[2]))

    def test_missing_relationship_preserves_earlier_changes(self):
        for missing in ('lo','qs','cs'):
            with self.subTest(missing=missing):
                actual=observe(docxtpl_native,missing=missing);self.assertEqual(actual,observe(docxtpl,missing=missing))
                self.assertEqual(actual[1],('KeyError',(None,)))

    def test_constructor_reset_override(self):
        def run(engine):
            module=importlib.import_module(engine.__name__+'.subdoc');trace=[];doc=Document()
            class Custom(module.SubdocComposer):
                def reset_reference_mapping(self):
                    trace.append((self.doc is doc,self.pkg is doc.part.package,self.restart_numbering))
            obj=Custom(doc)
            return trace,obj.first_section_properties_added,hasattr(obj,'num_id_mapping')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_no_diagrams(self):
        def run(engine):
            module=importlib.import_module(engine.__name__+'.subdoc');doc=Document();element=doc.add_paragraph('x')._p
            before=etree.tostring(element);composer=module.SubdocComposer(Document())
            return composer.add_diagrams(doc,element),before==etree.tostring(element)
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_no_runtime_import_and_xpath_literal_identity(self):
        import builtins,sys
        from unittest.mock import patch
        def run(engine):
            module=importlib.import_module(engine.__name__+'.subdoc');master=Document();child=Document()
            composer=module.SubdocComposer(master);queries=[];imports=[]
            class Element:
                def xpath(self,query,**kwargs): queries.append(query); return []
            original=builtins.__import__
            def observed(name,*args,**kwargs):
                if name in ('docxcompose.utils','docx.oxml.xmlchemy','docx.opc.constants'): imports.append(name)
                return original(name,*args,**kwargs)
            with patch('builtins.__import__',observed):
                composer.add_diagrams(child,Element());composer.add_diagrams(child,Element())
            return imports,queries[0] is queries[1],queries[0] is sys.intern('.//dgm:relIds[@r:dm]')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_diagram_method_lookup_precedes_namespace_access(self):
        import docxcompose.utils
        def run(engine):
            ns=docxcompose.utils.NS;original=ns['r'];trace=[];target=object()
            class Diagram:
                def __getattribute__(self,name):
                    if name=='get': trace.append('lookup_get');ns['r']='urn:get'
                    if name=='set': trace.append('lookup_set');ns['r']='urn:set'
                    return object.__getattribute__(self,name)
                def get(self,key): trace.append(('get',key));return 'rId'
                def set(self,key,value): trace.append(('set',key,value))
            diagram=Diagram()
            class Element:
                def xpath(self,query,**kwargs): return [diagram]
            class Rel: target_part=target
            class ChildPart: rels={'rId':Rel()}
            class Child: part=ChildPart()
            class MasterPart:
                package=object()
                def relate_to(self,target,kind): return 'new'
            class Master: part=MasterPart()
            try:
                module=importlib.import_module(engine.__name__+'.subdoc')
                module.SubdocComposer(Master()).add_diagrams(Child(),Element())
                return trace
            finally: ns['r']=original
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_xpath_helper_globals_can_be_rebound(self):
        import docxcompose.utils as utils
        def run(engine,mode):
            module=importlib.import_module(engine.__name__+'.subdoc');composer=module.SubdocComposer(Document());child=Document()
            trace=[]
            class Element:
                def xpath(self,query,**kwargs):
                    trace.append(('namespaces' in kwargs,kwargs.get('namespaces') is replacement));return []
            previous_ns=utils.NS;previous_base=utils.BaseOxmlElement;replacement=dict(previous_ns)
            try:
                if mode=='namespaces':utils.NS=replacement
                else:utils.BaseOxmlElement=Element
                composer.add_diagrams(child,Element())
                return trace
            finally:utils.NS=previous_ns;utils.BaseOxmlElement=previous_base
        for mode in ('namespaces','base'):
            with self.subTest(mode=mode):self.assertEqual(run(docxtpl_native,mode),run(docxtpl,mode))

    def test_subdoc_module_globals_are_dynamic(self):
        import sys
        def run(engine):
            module=importlib.import_module(engine.__name__+'.subdoc');trace=[];target=object()
            class Mapping(dict):
                def __getitem__(self,key):trace.append(('ns_key',key is sys.intern('r')));return super().__getitem__(key)
            class Types:
                DIAGRAM_DATA='data';DIAGRAM_LAYOUT='layout';DIAGRAM_QUICK_STYLE='quick';DIAGRAM_COLORS='colors'
            class Diagram:
                def get(self,key):trace.append(('get',key));return 'rid'
                def set(self,key,value):trace.append(('set',key,value))
            class Element:
                def xpath(self,query,**kwargs):return [Diagram()]
            class Rel: target_part=target
            class ChildPart: rels={'rid':Rel()}
            class Child: part=ChildPart()
            class MasterPart:
                package=object()
                def relate_to(self,target,kind):trace.append(('relate',kind));return 'new'
            class Master: part=MasterPart()
            had_ns=hasattr(module,'NS');had_rt=hasattr(module,'RT')
            old_ns=getattr(module,'NS',None);old_rt=getattr(module,'RT',None)
            try:
                module.NS=Mapping(r='urn:custom');module.RT=Types
                module.SubdocComposer(Master()).add_diagrams(Child(),Element())
                return trace
            finally:
                if had_ns:module.NS=old_ns
                else:del module.NS
                if had_rt:module.RT=old_rt
                else:del module.RT
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_composer_uses_its_own_module_globals(self):
        import importlib.util,sys
        def run(engine):
            original=importlib.import_module(engine.__name__+'.subdoc')
            name=engine.__name__+'._temporary_subdoc'
            spec=importlib.util.spec_from_file_location(name,original.__file__)
            module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
            trace=[];target=object()
            class Types:
                DIAGRAM_DATA='data';DIAGRAM_LAYOUT='layout';DIAGRAM_QUICK_STYLE='quick';DIAGRAM_COLORS='colors'
            class Diagram:
                def get(self,key):trace.append(('get',key));return 'rid'
                def set(self,key,value):trace.append(('set',key,value))
            class Element:
                def xpath(self,query,**kwargs):return [Diagram()]
            class Rel: target_part=target
            class ChildPart: rels={'rid':Rel()}
            class Child: part=ChildPart()
            class MasterPart:
                package=object()
                def relate_to(self,target,kind):trace.append(('relate',kind));return 'new'
            class Master: part=MasterPart()
            try:
                module.NS={'r':'urn:alias'};module.RT=Types
                module.SubdocComposer(Master()).add_diagrams(Child(),Element())
                return trace
            finally:del sys.modules[name]
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_reimported_module_has_own_xpath_literal(self):
        import importlib.util,sys
        def run(engine):
            original=importlib.import_module(engine.__name__+'.subdoc')
            name=engine.__name__+'._temporary_subdoc_query'
            spec=importlib.util.spec_from_file_location(name,original.__file__)
            alias=importlib.util.module_from_spec(spec);sys.modules[name]=alias;spec.loader.exec_module(alias)
            queries=[]
            class Element:
                def xpath(self,query,**kwargs): queries.append(query);return []
            try:
                for module in (original,original,alias,alias):
                    module.SubdocComposer(Document()).add_diagrams(Document(),Element())
                return queries[0] is queries[1],queries[2] is queries[3],queries[0] is queries[2]
            finally:del sys.modules[name]
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_diagrams_do_not_require_composer_module_in_sys_modules(self):
        import subprocess,sys,textwrap
        code=textwrap.dedent('''
            import sys,docxtpl
            from docx import Document
            sys.modules.pop('docxcompose.composer',None)
            import docxtpl_native.subdoc as native
            element=Document().add_paragraph('text')._p
            native.SubdocComposer(Document()).add_diagrams(Document(),element)
        ''')
        subprocess.run([sys.executable,'-c',code],check=True,capture_output=True,text=True)

    def test_missing_module_globals_fall_back_to_builtins(self):
        import builtins
        def run(engine):
            module=importlib.import_module(engine.__name__+'.subdoc');trace=[];target=object()
            class Types:
                DIAGRAM_DATA='data';DIAGRAM_LAYOUT='layout';DIAGRAM_QUICK_STYLE='quick';DIAGRAM_COLORS='colors'
            class Diagram:
                def get(self,key):trace.append(('get',key));return 'rid'
                def set(self,key,value):trace.append(('set',key,value))
            class Element:
                def xpath(self,query,**kwargs):return [Diagram()]
            class Rel: target_part=target
            class ChildPart: rels={'rid':Rel()}
            class Child: part=ChildPart()
            class MasterPart:
                package=object()
                def relate_to(self,target,kind):trace.append(('relate',kind));return 'new'
            class Master: part=MasterPart()
            old_ns=module.NS;old_rt=module.RT
            had_ns=hasattr(builtins,'NS');had_rt=hasattr(builtins,'RT')
            old_builtin_ns=getattr(builtins,'NS',None);old_builtin_rt=getattr(builtins,'RT',None)
            try:
                del module.NS;del module.RT;builtins.NS={'r':'urn:builtins'};builtins.RT=Types
                module.SubdocComposer(Master()).add_diagrams(Child(),Element())
                return trace
            finally:
                module.NS=old_ns;module.RT=old_rt
                if had_ns:builtins.NS=old_builtin_ns
                else:del builtins.NS
                if had_rt:builtins.RT=old_builtin_rt
                else:del builtins.RT
        self.assertEqual(run(docxtpl_native),run(docxtpl))

if __name__=='__main__': unittest.main()
