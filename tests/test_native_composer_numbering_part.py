"""Differential numbering part creation and user I/O behavior."""
import importlib,io,sys,unittest
from unittest.mock import patch
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
import docxcompose.composer as composer_module
import docxtpl,docxtpl_native

class NumberingPartTests(unittest.TestCase):
    def test_existing_part_identity(self):
        def run(engine):
            master=Document();expected=master.part.rels.part_with_reltype(RT.NUMBERING)
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            return obj.numbering_part() is expected
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_missing_part_creation(self):
        def run(engine):
            master=Document();rels=master.part.rels;rid=next(key for key,value in rels.items() if value.reltype==RT.NUMBERING);del rels[rid]
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master);part=obj.numbering_part()
            return str(part.partname),part.content_type,part.blob,part.package is master.part.package,master.part.rels.part_with_reltype(RT.NUMBERING) is part
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_user_open_protocol_and_handled_exception(self):
        def run(engine):
            master=Document();rels=master.part.rels;rid=next(key for key,value in rels.items() if value.reltype==RT.NUMBERING);del rels[rid];trace=[]
            class Stream:
                def read(self):trace.append(('read',type(sys.exception()).__name__));return b'<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'
            class Manager:
                def __enter__(self):trace.append(('enter',type(sys.exception()).__name__));return Stream()
                def __exit__(self,kind,value,tb):trace.append(('exit',kind,type(sys.exception()).__name__));return False
            def opened(path,mode):trace.append(('open',path,mode,type(sys.exception()).__name__));return Manager()
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            with patch('builtins.open',opened):part=obj.numbering_part()
            return trace,part.blob
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_suppressed_read_error_creates_unbound_error(self):
        def run(engine):
            master=Document();rels=master.part.rels;rid=next(key for key,value in rels.items() if value.reltype==RT.NUMBERING);del rels[rid];trace=[];marker=RuntimeError('fail')
            class Manager:
                def __enter__(self):return self
                def read(self):raise marker
                def __exit__(self,kind,value,tb):trace.append((value is marker,sys.exception() is marker));return True
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            try:
                with patch('builtins.open',lambda *args:Manager()):obj.numbering_part()
            except Exception as error:return type(error).__name__,str(error),trace,type(error.__context__).__name__
            raise AssertionError('error was suppressed unexpectedly')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_open_failure_keeps_original_exception_context(self):
        def run(engine):
            master=Document();rels=master.part.rels;rid=next(key for key,value in rels.items() if value.reltype==RT.NUMBERING);del rels[rid];trace=[];marker=RuntimeError('open failed')
            def opened(*args):trace.append(type(sys.exception()).__name__);raise marker
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            try:
                with patch('builtins.open',opened):obj.numbering_part()
            except Exception as error:return error is marker,type(error.__context__).__name__,trace
            raise AssertionError('open failure was ignored')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_cleanup_failure_preserves_nested_exception_context(self):
        def run(engine):
            master=Document();rels=master.part.rels;rid=next(key for key,value in rels.items() if value.reltype==RT.NUMBERING);del rels[rid]
            read_error=RuntimeError('read failed');exit_error=RuntimeError('exit failed');trace=[]
            class Manager:
                def __enter__(self):return self
                def read(self):raise read_error
                def __exit__(self,kind,value,tb):trace.append(sys.exception() is read_error);raise exit_error
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            try:
                with patch('builtins.open',lambda *args:Manager()):obj.numbering_part()
            except Exception as error:return error is exit_error,error.__context__ is read_error,type(read_error.__context__).__name__,trace
            raise AssertionError('cleanup failure was ignored')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_exception_class_override(self):
        def run(engine,replacement):
            master=Document();rels=master.part.rels;rid=next(key for key,value in rels.items() if value.reltype==RT.NUMBERING);del rels[rid]
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            try:
                with patch.object(composer_module,'KeyError',replacement,create=True):
                    part=obj.numbering_part();return 'ok',str(part.partname)
            except Exception as error:return type(error).__name__,str(error),type(error.__context__).__name__
        for replacement in (42,RuntimeError,(KeyError,ValueError)):
            with self.subTest(replacement=replacement):
                self.assertEqual(run(docxtpl_native,replacement),run(docxtpl,replacement))

    def test_missing_parser_precedes_unbound_value(self):
        def run(engine):
            master=Document();rels=master.part.rels;rid=next(key for key,value in rels.items() if value.reltype==RT.NUMBERING);del rels[rid]
            class Manager:
                def __enter__(self):return self
                def read(self):raise RuntimeError('suppressed')
                def __exit__(self,kind,value,tb):return True
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            original=composer_module.parse_xml
            try:
                del composer_module.parse_xml
                with patch('builtins.open',lambda *args:Manager()):obj.numbering_part()
            except Exception as error:return type(error).__name__,str(error),type(error.__context__).__name__
            finally:composer_module.parse_xml=original
            raise AssertionError('missing parser was ignored')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_none_read_result_is_not_treated_as_suppression(self):
        def run(engine):
            master=Document();rels=master.part.rels;rid=next(key for key,value in rels.items() if value.reltype==RT.NUMBERING);del rels[rid]
            class Manager:
                def __enter__(self):return self
                def read(self):return None
                def __exit__(self,kind,value,tb):return False
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            try:
                with patch('builtins.open',lambda *args:Manager()):obj.numbering_part()
            except Exception as error:return type(error).__name__,str(error),type(error.__context__).__name__
            raise AssertionError('invalid read result was accepted')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

if __name__=='__main__':unittest.main()
