"""Reference checks for exposed document setup and XML I/O protocols."""
import importlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import docxtpl
import docxtpl_native
from docx import Document


class DocumentBasicApiTests(unittest.TestCase):
    def test_render_init_order_identity_and_partial_failures(self):
        def run(engine, failing):
            events=[];error=RuntimeError('state failure');old_map={'old':1}
            class Subject:
                def __init__(self):object.__setattr__(self,'pic_map',old_map)
                def init_docx(self):
                    events.append('init')
                    if failing=='init':raise error
                def __setattr__(self,name,value):
                    events.append(('set',name))
                    if name==failing:raise error
                    object.__setattr__(self,name,value)
            subject=Subject()
            try:result=engine.DocxTemplate.render_init(subject); failure=None
            except RuntimeError as actual:result=None;failure=(actual is error,str(actual))
            state=subject.__dict__
            return (result,failure,events,state.get('pic_map') is old_map,
                    state.get('pic_map'),state.get('current_rendering_part','missing'),
                    state.get('docx_ids_index','missing'),state.get('is_saved','missing'))
        for failing in (None,'init','pic_map','current_rendering_part','docx_ids_index','is_saved'):
            with self.subTest(failing=failing):self.assertEqual(run(docxtpl_native,failing),run(docxtpl,failing))
        for engine in (docxtpl,docxtpl_native):
            class Subject:
                def init_docx(self):pass
            subject=Subject();engine.DocxTemplate.render_init(subject);first=subject.docx_ids_index
            engine.DocxTemplate.render_init(subject);self.assertIs(subject.docx_ids_index,first)

    def test_get_xml_preserves_lookup_order_and_object_identity(self):
        def run(engine):
            events=[];body=object();result=object()
            class Element:
                @property
                def body(self):events.append('body');return body
            class Doc:
                @property
                def _element(self):events.append('element');return Element()
            class Subject:
                @property
                def xml_to_string(self):
                    events.append('serializer_lookup')
                    def serialize(value):events.append(('serialize',value is body));return result
                    return serialize
                @property
                def docx(self):events.append('docx');return Doc()
            output=engine.DocxTemplate.get_xml(Subject())
            return output is result,events
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_write_xml_context_order_and_suppressed_errors(self):
        def run(engine,failure,suppress):
            module=importlib.import_module('docxtpl.template' if engine is docxtpl else 'docxtpl_native')
            events=[];error=RuntimeError('failed');filename=object();value=object()
            class File:
                @property
                def write(self):
                    events.append('write_lookup')
                    def write(arg):
                        events.append(('write',arg is value))
                        if failure=='write':raise error
                    return write
            class Manager:
                def __enter__(self):events.append('enter');return File()
                def __exit__(self,typ,exc,tb):
                    events.append(('exit',exc is error,sys.exception() is error if exc else sys.exception() is None))
                    return suppress
            class Subject:
                @property
                def get_xml(self):
                    events.append('xml_lookup')
                    def get():
                        events.append('xml')
                        if failure=='xml':raise error
                        return value
                    return get
            def opener(actual,mode):events.append(('open',actual is filename,mode));return Manager()
            with patch.object(module,'open',opener,create=True):
                try:result=engine.DocxTemplate.write_xml(Subject(),filename);failure_info=None
                except RuntimeError as actual:result=None;failure_info=(actual is error,str(actual))
            return result,failure_info,events
        for failure,suppress in ((None,False),('xml',False),('xml',True),('write',False),('write',True)):
            with self.subTest(failure=failure,suppress=suppress):self.assertEqual(run(docxtpl_native,failure,suppress),run(docxtpl,failure,suppress))

    def test_real_document_xml_file(self):
        def run(engine,folder):
            document=Document();document.add_paragraph('Text ✓');data=io.BytesIO();document.save(data)
            tpl=engine.DocxTemplate(io.BytesIO(data.getvalue()));tpl.render_init()
            path=Path(folder)/'body.xml';result=tpl.write_xml(path)
            return result,path.read_text(),tpl.get_xml()
        with tempfile.TemporaryDirectory() as folder:self.assertEqual(run(docxtpl_native,folder),run(docxtpl,folder))


if __name__ == '__main__':
    unittest.main()
