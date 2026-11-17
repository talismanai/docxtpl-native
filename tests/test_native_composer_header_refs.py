"""Reference removal on the caller-visible composition tree."""
import importlib
import unittest

from docx import Document
from docx.oxml import parse_xml
from lxml import etree
import docxtpl
import docxtpl_native

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
XML=f'<w:p xmlns:w="{W}" xmlns:r="{R}"><w:pPr><w:sectPr><w:headerReference r:id="rId1"/><w:footerReference r:id="rId2"/><w:pgSz w:w="1"/></w:sectPr></w:pPr></w:p>'


class NativeComposerHeaderReferenceTests(unittest.TestCase):
    def test_real_lxml_and_docx_elements(self):
        def run(engine,base):
            element=parse_xml(XML) if base else etree.fromstring(XML)
            composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(Document())
            result=composer.remove_header_and_footer_references(object(),element)
            return result,etree.tostring(element),len(element.xpath('.//w:headerReference|.//w:footerReference',namespaces={'w':W})) if not base else len(element.xpath('.//w:headerReference|.//w:footerReference'))
        for base in (False,True):
            with self.subTest(base=base):
                actual=run(docxtpl_native,base)
                self.assertEqual(actual,run(docxtpl,base));self.assertEqual(actual[2],0)

    def test_overridden_xpath_and_partial_failure(self):
        import docxcompose.composer as module
        def run(engine, fail):
            trace=[];marker=RuntimeError('remove failed');source=object();doc=object()
            class Parent:
                def remove(self,ref):
                    trace.append(('remove',ref.name))
                    if fail and ref.name=='second':raise marker
            parent=Parent()
            class Ref:
                def __init__(self,name):self.name=name
                def getparent(self):trace.append(('parent',self.name));return parent
            def callback(element,query):
                trace.append(('xpath',element is source,query))
                return iter([Ref('first'),Ref('second')])
            previous=module.xpath
            try:
                module.xpath=callback
                composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(Document())
                try:result=composer.remove_header_and_footer_references(doc,source);error=None
                except Exception as exc:result=None;error=(type(exc).__name__,exc.args,exc is marker)
                return result,error,trace
            finally:module.xpath=previous
        for fail in (False,True):
            with self.subTest(fail=fail):self.assertEqual(run(docxtpl_native,fail),run(docxtpl,fail))

    def test_rebound_utils_does_not_call_original_python_xpath(self):
        import subprocess,sys,textwrap
        code=textwrap.dedent('''
            import sys
            import docxcompose.composer as composer
            import docxcompose.utils as utils
            from docx import Document
            from lxml import etree
            original=composer.xpath
            utils.xpath=lambda *args: None
            utils.__file__='rebound-utils-path'
            import docxtpl_native.subdoc as native
            calls=[]
            def trace(frame,event,arg):
                if event=='call' and frame.f_code is original.__code__: calls.append('python xpath')
            sys.setprofile(trace)
            try: native.SubdocComposer(Document()).remove_header_and_footer_references(None,etree.fromstring('<root/>'))
            finally: sys.setprofile(None)
            assert not calls, calls
        ''')
        subprocess.run([sys.executable,'-c',code],check=True,capture_output=True,text=True)

    def test_decorated_xpath_override_is_called(self):
        import functools
        import docxcompose.composer as module
        original=module.xpath;calls=[]
        @functools.wraps(original)
        def callback(element,query): calls.append((element,query));return []
        try:
            module.xpath=callback
            obj=importlib.import_module('docxtpl_native.subdoc').SubdocComposer(Document())
            element=object();obj.remove_header_and_footer_references(None,element)
            self.assertEqual(len(calls),1);self.assertIs(calls[0][0],element)
        finally:module.xpath=original

    def test_composer_globals_survive_removal_from_sys_modules(self):
        import subprocess,sys,textwrap
        check=textwrap.dedent('''
            scope=native.Composer.remove_header_and_footer_references.__globals__
            old=scope['xpath'];calls=[]
            sys.modules.pop('docxcompose.composer',None)
            def callback(element,query): calls.append((element,query));return []
            scope['xpath']=callback
            try:
                element=etree.fromstring('<root/>')
                native.SubdocComposer(Document()).remove_header_and_footer_references(None,element)
                assert len(calls)==1 and calls[0][0] is element
            finally: scope['xpath']=old
        ''')
        setups={
            # The extension captures docxcompose.composer when the subdoc module loads.
            'captured':'import sys\nimport docxtpl_native.subdoc as native\n',
            # The extension loads first, before docxcompose.composer exists.
            'late':'import sys,docxcompose.utils,docxtpl_native\ndocxtpl_native._extension()\n'
                   'assert "docxcompose.composer" not in sys.modules\nimport docxtpl_native.subdoc as native\n',
        }
        for name,setup in setups.items():
            with self.subTest(setup=name):
                code='from docx import Document\nfrom lxml import etree\n'+setup+check
                result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__': unittest.main()
