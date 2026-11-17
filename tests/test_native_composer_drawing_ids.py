"""Drawing ID migration across the exposed document and related parts."""
import importlib
import unittest

from docx import Document
from docx.oxml import parse_xml
import docxtpl
import docxtpl_native

WP='http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'
PIC='http://schemas.openxmlformats.org/drawingml/2006/picture'


class NativeComposerDrawingIdsTests(unittest.TestCase):
    def test_real_body_header_and_footer(self):
        def run(engine):
            doc=Document()
            roots=(doc.add_paragraph()._p,doc.sections[0].header.add_paragraph()._p,doc.sections[0].footer.add_paragraph()._p)
            for root in roots:
                for tag,namespace in (('docPr',WP),('cNvPr',PIC)):
                    root.append(parse_xml(f'<x:{tag} xmlns:x="{namespace}" id="33" name="test"/>'))
            composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(doc)
            result=(composer.renumber_docpr_ids(),composer.renumber_nvpicpr_ids())
            return result,[[node.get('id') for node in root] for root in roots]
        actual=run(docxtpl_native)
        self.assertEqual(actual,run(docxtpl));self.assertEqual(actual[1],[['1','1'],['2','2'],['3','3']])

    def test_callback_order_and_partial_exception(self):
        import docxcompose.composer as module
        def run(engine,method,fail):
            trace=[];marker=RuntimeError('drawing id failed');body=object();header_element=object();footer_element=object()
            class Node:
                def __init__(self,name):self.name=name;self.value=None
                @property
                def id(self):return self.value
                @id.setter
                def id(self,value):
                    trace.append(('id',self.name,value))
                    if fail and self.name=='header':raise marker
                    self.value=value
            body_node=Node('body');header_node=Node('header');footer_node=Node('footer')
            class Part:
                def __init__(self,name,element):self.name=name;self._element=element
                @property
                def element(self):trace.append(('element',self.name));return self._element
            class Rel:
                def __init__(self,name,kind,part):self.name=name;self.kind=kind;self.part=part
                @property
                def reltype(self):trace.append(('reltype',self.name));return self.kind
                @property
                def target_part(self):trace.append(('target',self.name));return self.part
            relations=[Rel('header','header',Part('header',header_element)),Rel('ignored','other',None),Rel('footer','footer',Part('footer',footer_element))]
            class Relationships:
                def values(self):trace.append('values');return relations
            class MainPart:package=object();rels=Relationships()
            class Element:pass
            element=Element();element.body=body
            class Master:part=MainPart()
            master=Master();master.element=element
            class Types:HEADER='header';FOOTER='footer'
            def callback(element,query):
                trace.append(('xpath','body' if element is body else 'header' if element is header_element else 'footer',query))
                return [body_node if element is body else header_node if element is header_element else footer_node]
            old_xpath=module.xpath;old_rt=module.RT
            try:
                module.xpath=callback;module.RT=Types
                composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
                try:result=getattr(composer,method)();error=None
                except Exception as exc:result=None;error=(type(exc).__name__,exc.args,exc is marker)
                return result,error,trace,[node.value for node in (body_node,header_node,footer_node)]
            finally:module.xpath=old_xpath;module.RT=old_rt
        for method in ('renumber_docpr_ids','renumber_nvpicpr_ids'):
            for fail in (False,True):
                with self.subTest(method=method,fail=fail):self.assertEqual(run(docxtpl_native,method,fail),run(docxtpl,method,fail))


if __name__=='__main__': unittest.main()
