"""Extension ASTs compile to native instructions; Python is the current parser."""
import unittest

import docxtpl_native
import jinja2
from jinja2 import nodes
from jinja2.ext import Extension
from jinja2.lexer import Token
from docxtpl_native.bridge import BridgeConfig
from native_audit import forbid


class Echo(Extension):
    tags={'echo'}
    def parse(self, parser):
        token=next(parser.stream)
        return nodes.Output([parser.parse_expression()]).set_lineno(token.lineno)


class Assign(Extension):
    tags={'assign'}
    def parse(self, parser):
        token=next(parser.stream)
        return nodes.Assign(nodes.Name('generated_name','store'), nodes.Const(7)).set_lineno(token.lineno)


class Method(Extension):
    tags={'emit'}
    def parse(self, parser):
        token=next(parser.stream)
        return nodes.Output([self.call_method('_emit',[parser.parse_expression()])]).set_lineno(token.lineno)
    def _emit(self, value):
        self.environment.events.append(('emit',value))
        return '<'+str(value)+'>'


def render(native, source, context=None, *, extensions=(Echo,), autoescape=False, finalize=None, loader=None):
    environment=jinja2.Environment(extensions=extensions, autoescape=autoescape, finalize=finalize, loader=loader)
    environment.events=[]
    if native:
        config=BridgeConfig(environment,False)
        result=docxtpl_native._extension().PythonRenderSession(config,context or {}).render('case',source)
    else:result=environment.from_string(source).render(context or {})
    return result,environment.events


class ExtensionAstTests(unittest.TestCase):
    def test_expressions_use_shared_native_vm(self):
        expressions=('value','value.attr','value["attr"]','(2 + 3) * 7','-2 ** 3 + +4',
                     '1 < x < 9','x in [1, 3]','(1, 2, 3)[::2]', '"a" ~ x ~ "b"',
                     '{"a":x}.a','x if x else 9','x|int','x is defined','x and "yes" or "no"')
        for expression in expressions:
            source='{% echo '+expression+' %}'
            with self.subTest(expression=expression):
                self.assertEqual(render(True,source,{'x':3,'value':{'attr':'<X>'}},autoescape=True),
                                 render(False,source,{'x':3,'value':{'attr':'<X>'}},autoescape=True))

    def test_generated_names_and_loop_scope(self):
        source='{% assign %}{{ generated_name }}{% for x in [1,2] %}{% set generated_name=x %}{% echo generated_name %}{% endfor %}{{ generated_name }}'
        self.assertEqual(render(True,source,extensions=(Echo,Assign)),render(False,source,extensions=(Echo,Assign)))

    def test_control_flow_macros_capture_and_namespace(self):
        sources=(
            '{% if x==1 %}{% echo "a" %}{% elif x==2 %}{% echo "b" %}{% else %}{% echo "c" %}{% endif %}',
            '{% with a=x,b=x+1 %}{% echo a ~ b %}{% endwith %}{{ a is undefined }}',
            '{% set ns=namespace(n=0) %}{% for x in [1,2] %}{% set ns.n=ns.n+x %}{% endfor %}{% echo ns.n %}',
            '{% macro m(x=4) %}{% echo x+1 %}{% endmacro %}{{ m() }}{{ m(9) }}',
            '{% set text %}{% echo x %}{% endset %}{{ text }}',
            '{% filter upper %}{% echo "ab" %}{% endfilter %}',
            '{% autoescape true %}{% echo "<" ~ x %}{% endautoescape %}{% echo "<" ~ x %}',
            '{% macro wrap() %}[{{ caller() }}]{% endmacro %}{% call wrap() %}{% echo x %}{% endcall %}',
        )
        for source in sources:
            for x in (1,2,3):
                with self.subTest(source=source,x=x):self.assertEqual(render(True,source,{'x':x}),render(False,source,{'x':x}))

    def test_callback_and_autoescape(self):
        for autoescape in (False,True):
            source='{% emit "X" %}{% emit value %}'
            self.assertEqual(render(True,source,{'value':'&'},extensions=(Method,),autoescape=autoescape),
                             render(False,source,{'value':'&'},extensions=(Method,),autoescape=autoescape))

    def test_callback_exception_identity_and_effect_order(self):
        failure=ValueError('same extension exception')
        class Broken(Method):
            def _emit(self, value):raise failure
        environment=jinja2.Environment(extensions=[Broken]);events=[]
        session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{'later':lambda:events.append('later')})
        with self.assertRaises(ValueError) as actual:session.render('case','{% emit "X" %}{{ later() }}')
        self.assertIs(actual.exception,failure)
        self.assertEqual(events,[])

    def test_token_filter_transforms_data_and_expression(self):
        class Replace(Extension):
            def filter_stream(self,stream):
                for token in stream:
                    if token.type=='data':token=Token(token.lineno,'data',token.value.replace('before','after'))
                    elif token.type=='name' and token.value=='first':token=Token(token.lineno,'name','second')
                    yield token
        self.assertEqual(render(True,'before {{ first }}',{'first':1,'second':2},extensions=(Replace,)),
                         render(False,'before {{ first }}',{'first':1,'second':2},extensions=(Replace,)))

    def test_generated_constants_preserve_finalize_effects(self):
        def run(native):
            events=[]
            def finalize(value):events.append((type(value).__name__,str(value)));return '['+str(value)+']'
            output=render(native,'{% echo 2+3 %}|{% echo x %}',{'x':'&'},finalize=finalize,autoescape=True)
            return output,events
        self.assertEqual(run(True),run(False))

    def test_loaded_extension_templates_have_owned_storage(self):
        loader=jinja2.DictLoader({'included':'{% echo x %}', 'base':'A{% block content %}base{% endblock %}Z'})
        for source in ('{% include "included" %}', '{% extends "base" %}{% block content %}{% echo x %}{% endblock %}'):
            self.assertEqual(render(True,source,{'x':'owned'},loader=loader),render(False,source,{'x':'owned'},loader=loader))

    def test_method_is_resolved_at_execution_after_mutation(self):
        def run(native):
            environment=jinja2.Environment(extensions=[Method]);environment.events=[]
            extension=next(iter(environment.extensions.values()))
            def mutate():extension._emit=lambda value:'changed:'+str(value);return ''
            source='{{ mutate() }}{% emit 3 %}'
            if native:return docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{'mutate':mutate}).render('case',source)
            return environment.from_string(source).render(mutate=mutate)
        self.assertEqual(run(True),run(False))

    def test_native_compiler_does_not_call_python_codegen_or_render(self):
        environment=jinja2.Environment(extensions=[Echo,Assign,Method]);environment.events=[]
        session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{})
        with forbid('jinja2.compiler.CodeGenerator.visit','jinja2.Template.render',message='Python codegen or renderer invoked'):
            self.assertEqual(session.render('case','{% assign %}{% echo generated_name %}{% emit "x" %}'),'7<x>')


if __name__=='__main__':unittest.main()
