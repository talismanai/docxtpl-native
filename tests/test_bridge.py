import io
import unittest
from unittest.mock import patch

import jinja2
from docx import Document
from docxtpl_native import DocxTemplate, UnsupportedFeature, capabilities
from docxtpl_native.bridge import BridgeConfig
from native_audit import forbid


class ConfigurationTests(unittest.TestCase):
    def test_subclasses_keep_the_original_environment(self):
        class CustomEnvironment(jinja2.Environment):
            pass
        environment=CustomEnvironment()
        self.assertIs(BridgeConfig(environment,False).environment,environment)

    def test_decorated_callback_receives_original_environment(self):
        environment=jinja2.Environment()
        config=BridgeConfig(environment, False)
        @jinja2.pass_environment
        def callback(actual, value):
            self.assertIs(actual, environment)
            return value
        marker=object()
        self.assertIs(config.invoke(callback,[marker],{}, {},False), marker)

    def test_pass_context_uses_real_jinja_context(self):
        config=BridgeConfig(None,False)
        @jinja2.pass_context
        def callback(context):
            self.assertIsInstance(context,jinja2.runtime.Context)
            return context['name']
        self.assertEqual(config.invoke(callback,[],{}, {'name':'Rust'},False),'Rust')

    def test_instance_lookup_hook_is_not_ignored(self):
        environment=jinja2.Environment();environment.getattr=lambda value,name:'custom'
        self.assertEqual(BridgeConfig(environment,False).lookup(object(),'name',True),'custom')

    def test_compiler_override_is_not_ignored(self):
        environment=jinja2.Environment();environment.compile=lambda *args:None
        with self.assertRaises(UnsupportedFeature):BridgeConfig(environment,False)

    def test_bytecode_cache_is_not_ignored(self):
        with self.assertRaises(UnsupportedFeature):
            BridgeConfig(jinja2.Environment(bytecode_cache=jinja2.FileSystemBytecodeCache()),False)

    def test_closed_source_grammar_rejects_operators_and_unknown_names(self) -> None:
        from docxtpl_native.simple_frontend import accepts,context_names
        names=context_names({'value':'X','not':'Y'})
        self.assertTrue(accepts('Café {{ value }}! {{\nvalue\t}}',names))
        self.assertTrue(accepts('{% if value %}X{% endif %}', names))
        # Standard filters on a value, with literal arguments, are in the grammar.
        self.assertTrue(accepts('{{ value|upper }}', names))
        self.assertTrue(accepts('{% if value|upper %}X{% endif %}', names))
        for source in ['{{ not }}','{{ missing }}','{{ value|nosuch }}','{{- value }}',
                       '{{ value + }}', '{% if missing %}X{% endif %}',
                       "{% if value|replace('a', 'b') %}X{% endif %}", '{# comment #}']:
            with self.subTest(source=source):self.assertFalse(accepts(source,names))
        self.assertIsNone(context_names({'value':object()}))

    def test_simple_context_names_validate_nested_types_and_cycles(self):
        from docxtpl_native.simple_frontend import context_names
        class DictSubclass(dict): pass
        class StringSubclass(str): pass
        class ListSubclass(list): pass
        cyclic=[]; cyclic.append(cyclic)
        shared={'nested':[None,True,42,1.5,b'bytes','\ud800',cyclic]}
        context={'first':shared,'second':shared,'\ud800':()}
        self.assertEqual(context_names(context),frozenset(context))
        for value in (DictSubclass(), {'nested':DictSubclass()},
                      {'nested':{1:'value'}}, {StringSubclass('key'):'value'},
                      {'nested':ListSubclass()}, {'nested':object()}):
            with self.subTest(value=value):
                self.assertIsNone(context_names(value))

    def test_simple_context_names_observe_metaclass_comparison(self):
        from docxtpl_native.simple_frontend import context_names
        trace=[]
        class ScalarMeta(type):
            def __eq__(cls, other):
                trace.append(other)
                return other is str
        class Scalar(metaclass=ScalarMeta): pass
        self.assertIsNone(context_names({'value':Scalar()}))
        self.assertEqual(trace,[str])
        trace.clear()
        class OrdinaryMeta(type):
            def __eq__(cls, other):
                trace.append(other)
                return False
        class Ordinary(metaclass=OrdinaryMeta): pass
        self.assertIsNone(context_names({'value':Ordinary()}))
        self.assertEqual(trace,[str,int,float,bool,bytes,type(None)])
        failure=RuntimeError('comparison failed')
        class BrokenMeta(type):
            def __eq__(cls, other): raise failure
        class Broken(metaclass=BrokenMeta): pass
        with self.assertRaises(RuntimeError) as caught:
            context_names({'value':Broken()})
        self.assertIs(caught.exception,failure)

    def test_complex_source_keeps_original_validation_and_disables_the_fast_path(self):
        config=BridgeConfig(None,False);config.enable_simple_sources({'value':'X'})
        config.validate_source('simple','{{ value }}')
        self.assertEqual(config.native_frontend_sources,1)
        with self.assertRaises(jinja2.TemplateSyntaxError):config.validate_source('invalid','{{ value + }}')
        config.validate_source('later','{{ value }}')
        self.assertEqual(config.native_frontend_sources,1)
        self.assertIn('later',config._compile_sources)


@unittest.skipUnless(capabilities()['native_available'], 'native extension not built; not a parity pass')
class NativeBridgeTests(unittest.TestCase):
    def render(self, text, context, environment=None):
        document=Document();document.add_paragraph(text)
        source=io.BytesIO();document.save(source)
        template=DocxTemplate(io.BytesIO(source.getvalue()))
        # A native render must never call Jinja2's Python template executor,
        # even for a custom Environment.
        with forbid('jinja2.Template.render',message='Python template executor called'):
            template.render(context,jinja_env=environment)
        result=io.BytesIO();template.save(result)
        result.seek(0)
        return Document(result).paragraphs[0].text,template

    def test_live_object_callback_and_identity(self):
        marker=object()
        def callback(value):
            self.assertIs(value,marker)
            return 'same'
        text,template=self.render('{{ callback(marker) }}',{'callback':callback,'marker':marker})
        self.assertEqual(text,'same')
        self.assertEqual(template.last_execution['renderer'],'native')
        self.assertGreater(template.last_execution['python_callback_calls'],0)

    def test_bare_context_names_use_the_closed_native_frontend(self):
        text,template=self.render('Café {{ value }}! {{ value }}',{'value':'X'})
        self.assertEqual(text,'Café X! X')
        self.assertGreater(template.last_execution['native_frontend_sources'],0)

    def test_decorated_filter_and_loader(self):
        environment=jinja2.Environment(loader=jinja2.DictLoader({'fragment':'{{ value|prefix }}'}))
        @jinja2.pass_context
        def prefix(context,value):
            return context['prefix']+value
        environment.filters['prefix']=prefix
        text,_=self.render("{% include 'fragment' %}",{'value':'X','prefix':'P'},environment)
        self.assertEqual(text,'PX')

    def test_callback_exception_instance_survives(self):
        class BusinessError(Exception):
            pass
        error=BusinessError('original')
        def callback():
            raise error
        with self.assertRaises(BusinessError) as captured:
            self.render('{{ callback() }}',{'callback':callback})
        self.assertIs(captured.exception,error)

    def test_mutations_are_visible_to_later_lookups(self):
        data={'value':'before'}
        def change():
            data['value']='after'
            return ''
        text,_=self.render('{{ data.value }}{{ change() }}{{ data.value }}',{'data':data,'change':change})
        self.assertEqual(text,'beforeafter')

    def test_finalizer(self):
        text,_=self.render('{{ value }}',{'value':None},jinja2.Environment(finalize=lambda value:'EMPTY' if value is None else value))
        self.assertEqual(text,'EMPTY')

    def test_literal_list_mutation_and_tuple_type_use_native_execution(self):
        def kind(value):return type(value).__name__
        text,_=self.render("{% set items=[] %}{{ items.append('X') }}{{ items }}|{{ kind((1,2)) }}",{'kind':kind})
        self.assertEqual(text,"None['X']|tuple")

    def test_constant_filter_runs_before_native_runtime_callback(self):
        trace=[];environment=jinja2.Environment()
        def mark(value):trace.append('constant');return value
        def runtime():trace.append('runtime');return ''
        environment.filters['mark']=mark
        text,_=self.render("{{ runtime() }}{{ 'X'|mark }}",{'runtime':runtime},environment)
        self.assertEqual((text,trace),('X',['constant','runtime']))

    def test_enabling_a_finalizer_does_not_change_the_compiled_body(self):
        environment=jinja2.Environment();trace=[]
        def enable():environment.finalize=lambda value:'changed';trace.append('enable');return 'E'
        def disable():environment.finalize=None;trace.append('disable');return 'D'
        text,_=self.render('{{ enable() }}|{{ value }}|{{ disable() }}',
                           {'enable':enable,'disable':disable,'value':'X'},environment)
        self.assertEqual((text,trace),('E|X|D',['enable','disable']))

    def test_rich_values_are_live_python_objects_in_native_execution(self):
        from docxtpl_native import RichText, Listing
        rich=RichText('A');listing=Listing('before')
        def change(actual):
            self.assertIs(actual,rich)
            actual.add('B',bold=True);listing.xml='after'
            return RichText('C',italic=True)
        text,_=self.render('{{r rich }}{{r change(rich) }}{{r rich }}|{{ listing }}',
                           {'rich':rich,'listing':listing,'change':change},jinja2.Environment(autoescape=True))
        self.assertEqual(text,'ACAB|after')

    def test_exposed_document_model_uses_native_template_execution(self):
        from docxtpl_native import DocxTemplate
        from paritylab.protocol_base import template
        document=DocxTemplate(io.BytesIO(template('{{ callback() }}','{{ callback() }}')))
        live=document.get_docx();old=live.paragraphs[0];body=live._element.body;trace=[]
        def callback():trace.append(live._element.body is body);return 'X'
        with forbid('jinja2.Template.render',message='Python template executor called'):
            document.render({'callback':callback})
        self.assertEqual(trace,[True,False])
        self.assertEqual(live._element.body.xpath('string(.)'),'X')
        self.assertIs(live.paragraphs[0]._element,old._element)
        self.assertEqual(old.text,'{{ callback() }}')
        self.assertEqual(document.last_execution['renderer'],'native')
        self.assertEqual(document.last_execution['document_model'],'python-docx/lxml')


class StandardExtensionConfigurationTests(unittest.TestCase):
    def test_standard_do_and_loopcontrols_use_original_environment(self):
        env=jinja2.Environment(extensions=['jinja2.ext.do','jinja2.ext.loopcontrols'])
        self.assertIs(BridgeConfig(env,False).environment,env)

    def test_custom_extension_subclass_retains_ast_for_native_compiler(self):
        from jinja2.ext import ExprStmtExtension
        class CustomDo(ExprStmtExtension):
            pass
        config=BridgeConfig(jinja2.Environment(extensions=[CustomDo]),False)
        self.assertEqual(config.validate_source('case','{% do func() %}'),(False,False))
        self.assertEqual(type(config._compile_trees['case'].body[0]).__name__,'ExprStmt')

    def test_mutated_standard_extension_retains_ast_for_native_compiler(self):
        env=jinja2.Environment(extensions=['jinja2.ext.do'])
        extension=next(iter(env.extensions.values()))
        extension.custom=True
        config=BridgeConfig(env,False)
        self.assertEqual(config.validate_source('case','{{ value }}'),(False,False))
        self.assertEqual(type(config._compile_trees['case'].body[0]).__name__,'Output')

    def test_custom_extension_parser_exception_is_preserved(self):
        from jinja2.ext import Extension
        failure=ValueError('extension parser failed');events=[]
        class Broken(Extension):
            tags={'broken'}
            def parse(self,parser):
                events.append('parse');raise failure
        config=BridgeConfig(jinja2.Environment(extensions=[Broken]),False)
        with self.assertRaises(ValueError) as actual:config.validate_source('case','{% broken %}')
        self.assertIs(actual.exception,failure)
        self.assertEqual(events,['parse'])

    def test_custom_extension_preprocess_exception_is_preserved(self):
        from jinja2.ext import Extension
        failure=ValueError('preprocess failed');events=[]
        class Broken(Extension):
            def preprocess(self,source,name,filename=None):
                events.append(source);raise failure
        config=BridgeConfig(jinja2.Environment(extensions=[Broken]),False)
        with self.assertRaises(ValueError) as actual:config.validate_source('case','hello')
        self.assertIs(actual.exception,failure)
        self.assertEqual(events,['hello'])


class AsyncConfigurationTests(unittest.TestCase):
    def test_async_source_can_be_compiled_before_running_the_task(self):
        config=BridgeConfig(jinja2.Environment(enable_async=True),False)
        self.assertEqual(config.validate_source('case','{{ callback() }}'),(False,False))

    def test_running_event_loop_reports_reference_error_without_callback(self):
        import asyncio
        events=[]
        async def run():
            config=BridgeConfig(jinja2.Environment(enable_async=True),False)
            with self.assertRaisesRegex(RuntimeError,'asyncio.run\\(\\) cannot be called from a running event loop'):
                from docxtpl_native import _extension
                _extension().PythonRenderSession(config,{'callback':lambda:events.append('callback')}).render('case','{{ callback() }}')
            self.assertEqual(events,[])
        asyncio.run(run())

    def test_async_syntax_error_precedes_running_loop_error(self):
        import asyncio
        source='{{ value + }}'
        def reference():
            try:jinja2.Environment(enable_async=True).from_string(source)
            except jinja2.TemplateSyntaxError as error:return error.message,error.lineno
            self.fail('expected syntax error')
        async def run():
            config=BridgeConfig(jinja2.Environment(enable_async=True),False)
            with self.assertRaises(jinja2.TemplateSyntaxError) as actual:config.validate_source('case',source)
            self.assertEqual((actual.exception.message,actual.exception.lineno),reference())
        asyncio.run(run())

    def test_async_loop_lookup_error_preserves_identity(self):
        failure=ValueError('loop lookup failed')
        config=BridgeConfig(jinja2.Environment(enable_async=True),False)
        with patch('asyncio.events._get_running_loop',side_effect=failure):
            from docxtpl_native import _extension
            with self.assertRaises(ValueError) as actual:
                _extension().PythonRenderSession(config,{}).render('case','{{ callback() }}')
        self.assertIs(actual.exception,failure)
