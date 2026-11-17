"""Reference protocols for undeclared-variable analysis."""
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import docxtpl
import docxtpl_native


class AnalysisApiTests(unittest.TestCase):
    def test_document_relationships_callbacks_and_context_order(self):
        def run(engine,with_context):
            module=importlib.import_module('docxtpl.template' if engine is docxtpl else 'docxtpl_native')
            trace=[];template=object();body=object();parsed_header=object();parsed_footer=object();tree=object()
            class Part:
                def __init__(self,label):self.label=label
                @property
                def blob(self):trace.append('blob:'+self.label);return self.label.encode()
            class Rel:
                def __init__(self,kind,label):self.kind,self.label=kind,label
                @property
                def reltype(self):trace.append('type:'+self.label);return self.kind
                @property
                def target_part(self):trace.append('part:'+self.label);return Part(self.label)
            class Rels:
                def items(self):trace.append('items');return iter([('h',Rel('header','h')),('f',Rel('footer','f'))])
            class Temp:
                _element=SimpleNamespace(body=body)
                _part=SimpleNamespace(rels=Rels())
            def document(value):trace.append(('Document',value is template));return Temp()
            def parse(value):trace.append(('parse_xml',value.decode()));return parsed_header if value==b'h' else parsed_footer
            class Subject:
                template_file=template
                @property
                def HEADER_URI(self):trace.append('header_uri');return 'header'
                @property
                def FOOTER_URI(self):trace.append('footer_uri');return 'footer'
                def xml_to_string(self,value):trace.append('serialize');return 'B' if value is body else 'H' if value is parsed_header else 'F'
                def patch_xml(self,value):trace.append(('patch',value));return '['+value+']'
            class Environment:
                def __bool__(self):trace.append('env_bool');return True
                def parse(self,value):trace.append(('parse',value));return tree
            class Meta:
                def find_undeclared_variables(self,value):trace.append(('find',value is tree));return {'one','two'}
            class Context:
                def keys(self):trace.append('keys');return ['two']
            with patch.object(module,'Document',document,create=True),patch.object(module,'parse_xml',parse,create=True),patch.object(module,'meta',Meta(),create=True):
                result=engine.DocxTemplate.get_undeclared_template_variables(Subject(),Environment(),Context() if with_context else None)
            return result,trace
        for with_context in (False,True):
            with self.subTest(with_context=with_context):self.assertEqual(run(docxtpl_native,with_context),run(docxtpl,with_context))

    def test_callback_failures_keep_identity_and_stop(self):
        def run(engine,failure):
            module=importlib.import_module('docxtpl.template' if engine is docxtpl else 'docxtpl_native')
            events=[];error=RuntimeError('failure');body=object()
            class Subject:
                template_file='source'
                HEADER_URI='h';FOOTER_URI='f'
                def xml_to_string(self,value):events.append('serialize');return 'xml'
                def patch_xml(self,value):events.append('patch');return value
            class Rels:
                def items(self):events.append('items');return iter(())
            def document(value):events.append('document');return SimpleNamespace(_element=SimpleNamespace(body=body),_part=SimpleNamespace(rels=Rels()))
            class Env:
                def __bool__(self):events.append('env_bool');return True
                def parse(self,value):events.append('parse');raise error if failure=='parse' else AssertionError('unexpected')
            with patch.object(module,'Document',document,create=True):
                try:engine.DocxTemplate.get_undeclared_template_variables(Subject(),Env())
                except RuntimeError as actual:return actual is error,events
            raise AssertionError('expected callback failure')
        self.assertEqual(run(docxtpl_native,'parse'),run(docxtpl,'parse'))

    def test_malformed_relationship_pairs_match_reference(self):
        def run(engine,item):
            module=importlib.import_module('docxtpl.template' if engine is docxtpl else 'docxtpl_native')
            class Rels:
                def items(self):return iter([item])
            class Subject:
                template_file='source';HEADER_URI='h';FOOTER_URI='f'
                def xml_to_string(self,value):return 'xml'
                def patch_xml(self,value):return value
            def document(value):return SimpleNamespace(_element=SimpleNamespace(body=object()),_part=SimpleNamespace(rels=Rels()))
            with patch.object(module,'Document',document,create=True):
                try:engine.DocxTemplate.get_undeclared_template_variables(Subject())
                except Exception as error:return type(error),str(error)
            self.fail('expected relationship unpacking failure')
        for item in (None,1,(),('x',),('a','b','c')):
            with self.subTest(item=item):self.assertEqual(run(docxtpl_native,item),run(docxtpl,item))

    def test_falsey_environment_and_rebound_set(self):
        def run(engine):
            module=importlib.import_module('docxtpl.template' if engine is docxtpl else 'docxtpl_native')
            trace=[];tree=object();sentinel=object()
            class Rels:
                def items(self):return iter(())
            class Subject:
                template_file='source';HEADER_URI='h';FOOTER_URI='f'
                def xml_to_string(self,value):return 'xml'
                def patch_xml(self,value):return value
            def document(value):return SimpleNamespace(_element=SimpleNamespace(body=object()),_part=SimpleNamespace(rels=Rels()))
            class FalseEnvironment:
                def __bool__(self):trace.append('bool');return False
            class Environment:
                def parse(self,source):trace.append(('parse',source));return tree
            def environment():trace.append('environment');return Environment()
            class Variables:
                def __sub__(self,value):trace.append(('subtract',value is sentinel));return 'result'
            class Meta:
                def find_undeclared_variables(self,value):trace.append(('meta',value is tree));return Variables()
            class Context:
                def keys(self):trace.append('keys');return ['key']
            def make_set(value):trace.append(('set',list(value)));return sentinel
            with patch.object(module,'Document',document,create=True),patch.object(module,'Environment',environment,create=True),patch.object(module,'meta',Meta(),create=True),patch.object(module,'set',make_set,create=True):
                result=engine.DocxTemplate.get_undeclared_template_variables(Subject(),FalseEnvironment(),Context())
            return result,trace
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_result_identity_without_context(self):
        for engine in (docxtpl,docxtpl_native):
            with self.subTest(engine=engine.__name__):
                module=importlib.import_module('docxtpl.template' if engine is docxtpl else 'docxtpl_native')
                result=object();tree=object()
                class Rels:
                    def items(self):return iter(())
                class Subject:
                    template_file='source';HEADER_URI='h';FOOTER_URI='f'
                    def xml_to_string(self,value):return 'xml'
                    def patch_xml(self,value):return value
                document=lambda value:SimpleNamespace(_element=SimpleNamespace(body=object()),_part=SimpleNamespace(rels=Rels()))
                environment=SimpleNamespace(parse=lambda source:tree)
                meta=SimpleNamespace(find_undeclared_variables=lambda value:result)
                with patch.object(module,'Document',document,create=True),patch.object(module,'meta',meta,create=True):
                    self.assertIs(engine.DocxTemplate.get_undeclared_template_variables(Subject(),environment),result)


if __name__ == '__main__':unittest.main()
