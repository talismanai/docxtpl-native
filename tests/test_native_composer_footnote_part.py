"""Differential footnote part creation and user I/O behavior."""
import importlib,sys,unittest
from unittest.mock import patch
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
import docxcompose.composer as composer_module
import docxtpl,docxtpl_native

class FootnotePartTests(unittest.TestCase):
    def test_existing_part_identity(self):
        def run(engine):
            master=Document();part=Part(PackURI('/word/footnotes.xml'),'application/xml',b'<f/>',master.part.package)
            master.part.relate_to(part,RT.FOOTNOTES)
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            return obj.footnote_part() is part
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_missing_part_creation(self):
        def run(engine):
            master=Document();obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master);part=obj.footnote_part()
            return str(part.partname),part.content_type,part.blob,part.package is master.part.package,master.part.rels.part_with_reltype(RT.FOOTNOTES) is part
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_user_open_protocol_and_handled_exception(self):
        def run(engine):
            master=Document();trace=[]
            class Stream:
                def read(self):trace.append(('read',type(sys.exception()).__name__));return b'<footnotes/>'
            class Manager:
                def __enter__(self):trace.append(('enter',type(sys.exception()).__name__));return Stream()
                def __exit__(self,kind,value,tb):trace.append(('exit',kind,type(sys.exception()).__name__));return False
            def opened(path,mode):trace.append(('open',path,mode,type(sys.exception()).__name__));return Manager()
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            with patch('builtins.open',opened):part=obj.footnote_part()
            return trace,part.blob
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_suppression_uses_part_global_before_unbound_bytes(self):
        def run(engine):
            master=Document();trace=[];original=composer_module.Part
            class Manager:
                def __enter__(self):return self
                def read(self):raise RuntimeError('suppressed')
                def __exit__(self,kind,value,tb):trace.append(type(sys.exception()).__name__);return True
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            try:
                del composer_module.Part
                with patch('builtins.open',lambda *args:Manager()):obj.footnote_part()
            except Exception as error:return type(error).__name__,str(error),type(error.__context__).__name__,trace
            finally:composer_module.Part=original
            raise AssertionError('missing Part global was ignored')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_cleanup_failure_preserves_nested_exception_context(self):
        def run(engine):
            master=Document();read_error=RuntimeError('read failed');exit_error=RuntimeError('exit failed');trace=[]
            class Manager:
                def __enter__(self):return self
                def read(self):raise read_error
                def __exit__(self,kind,value,tb):trace.append(sys.exception() is read_error);raise exit_error
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            try:
                with patch('builtins.open',lambda *args:Manager()):obj.footnote_part()
            except Exception as error:return error is exit_error,error.__context__ is read_error,type(read_error.__context__).__name__,trace
            raise AssertionError('cleanup failure was ignored')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_user_part_factory_receives_raw_bytes_and_active_exception(self):
        def run(engine):
            master=Document();trace=[];original=composer_module.Part
            def factory(name,content,blob,package):
                trace.append((str(name),content,type(blob).__name__,package is master.part.package,type(sys.exception()).__name__))
                return original(name,content,blob,package)
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            with patch.object(composer_module,'Part',factory):part=obj.footnote_part()
            return trace,part.blob
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_footnote_creation_does_not_require_parser(self):
        def run(engine):
            master=Document();obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master);original=composer_module.parse_xml
            try:
                del composer_module.parse_xml
                part=obj.footnote_part();return str(part.partname),part.blob
            finally:composer_module.parse_xml=original
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_custom_relationship_and_content_type_lookups(self):
        def run(engine,kind):
            master=Document();trace=[];original_rt=composer_module.RT;original_ct=composer_module.CT
            if kind=='numbering':
                rels=master.part.rels;rid=next(key for key,value in rels.items() if value.reltype==RT.NUMBERING);del rels[rid]
            class Constants:
                def __init__(self,original,label):self.original=original;self.label=label
                def __getattribute__(self,name):
                    if name in ('original','label'):return object.__getattribute__(self,name)
                    trace.append((self.label,name,name is sys.intern(name)))
                    return getattr(self.original,name)
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            with patch.object(composer_module,'RT',Constants(original_rt,'rt')),patch.object(composer_module,'CT',Constants(original_ct,'ct')):
                part=getattr(obj,'numbering_part' if kind=='numbering' else 'footnote_part')()
            return trace,str(part.partname),part.content_type
        for kind in ('numbering','footnote'):
            with self.subTest(kind=kind):self.assertEqual(run(docxtpl_native,kind),run(docxtpl,kind))

if __name__=='__main__':unittest.main()
