"""Styles referenced by other document parts, compared with docxtpl."""
import importlib
import unittest
from unittest.mock import patch
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from lxml import etree
import docxcompose.composer as composer_module
import docxtpl
import docxtpl_native


def run(engine,content=None,parse_override=None,style_error=None):
    parent=Document();child=Document();trace=[]
    if content is not None:
        part=Part(PackURI('/word/footnotes.xml'),'application/xml',content,child.part.package)
        child.part.relate_to(part,RT.FOOTNOTES)
    cls=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
    class Composer(cls):
        def add_styles(self,doc,element):
            trace.append(('styles',doc is child,etree.tostring(element)))
            if style_error is not None:raise style_error
    composer=Composer(parent)
    try:
        with patch.object(composer_module,'parse_xml',parse_override or composer_module.parse_xml):
            result=composer.add_styles_from_other_parts(child)
        error=None
    except Exception as exc:
        result=None;error=(type(exc).__name__,exc.args,exc is style_error)
    return result,error,trace

class NativeComposerOtherStylesTests(unittest.TestCase):
    def test_existing_missing_and_malformed_part(self):
        for content in (None,b'<root><style/></root>',b'<bad'):
            with self.subTest(content=content):
                self.assertEqual(run(docxtpl_native,content),run(docxtpl,content))

    def test_custom_parser_and_suppressed_errors(self):
        for error in (KeyError('missing'),ValueError('invalid'),RuntimeError('other')):
            with self.subTest(error=error):
                def parse(value):raise error
                self.assertEqual(run(docxtpl_native,b'<root/>',parse),run(docxtpl,b'<root/>',parse))

    def test_style_errors_are_not_suppressed(self):
        for error in (KeyError('from styles'),ValueError('from styles'),RuntimeError('from styles')):
            with self.subTest(error=error):
                actual=run(docxtpl_native,b'<root/>',style_error=error)
                self.assertEqual(actual,run(docxtpl,b'<root/>',style_error=error))
                self.assertTrue(actual[1][2])

    def test_invalid_exception_class_preserves_context(self):
        marker=ValueError('invalid document')
        def execute(engine,name):
            parent=Document();child=Document()
            part=Part(PackURI('/word/footnotes.xml'),'application/xml',b'<bad',child.part.package)
            child.part.relate_to(part,RT.FOOTNOTES)
            instance=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(parent)
            def parse(blob):raise marker
            try:
                with patch.object(composer_module,name,42,create=True),patch.object(composer_module,'parse_xml',parse):
                    instance.add_styles_from_other_parts(child)
            except Exception as error:
                return type(error).__name__,str(error),error.__context__ is marker
            raise AssertionError('expected an invalid exception class failure')
        for name in ('KeyError','ValueError'):
            with self.subTest(name=name):
                self.assertEqual(execute(docxtpl_native,name),execute(docxtpl,name))

if __name__=='__main__':unittest.main()
