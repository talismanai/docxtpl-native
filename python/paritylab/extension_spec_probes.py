"""Reference observations for extension-generated syntax and AST nodes."""
import jinja2
from jinja2 import nodes
from jinja2.ext import Extension
from jinja2.lexer import Token
from .protocol_base import Probe,Result,generate,observed


def extension_expression(engine):
    class Echo(Extension):
        tags={'echoexpr'}
        def parse(self,parser):
            token=next(parser.stream);expression=parser.parse_expression()
            return nodes.Output([expression]).set_lineno(token.lineno)
    return observed(generate(engine,'{% echoexpr value %}',{'value':'X'},jinja2.Environment(extensions=[Echo])))


def extension_method(engine):
    trace=[]
    class Emit(Extension):
        tags={'emitvalue'}
        def parse(self,parser):
            token=next(parser.stream)
            return nodes.Output([self.call_method('_emit',[nodes.Const('X')])]).set_lineno(token.lineno)
        def _emit(self,value):trace.append(value);return '<'+value+'>'
    return observed(generate(engine,'{% emitvalue %}',{},jinja2.Environment(extensions=[Emit],autoescape=True)),trace)


def extension_assignment(engine):
    class Assign(Extension):
        tags={'assignvalue'}
        def parse(self,parser):
            token=next(parser.stream)
            return nodes.Assign(nodes.Name('n','store'),nodes.Const(7)).set_lineno(token.lineno)
    return observed(generate(engine,'{% assignvalue %}{{ n }}',{},jinja2.Environment(extensions=[Assign])))


def extension_parse_exception(engine):
    trace=[];failure=ValueError('extension parser failed')
    class Broken(Extension):
        tags={'broken'}
        def parse(self,parser):trace.append('parse');raise failure
    try:generate(engine,'{% broken %}',{},jinja2.Environment(extensions=[Broken]))
    except ValueError as error:return Result({'same_instance':error is failure,'message':str(error),'trace':trace},{})
    raise AssertionError('expected extension parser failure')


def extension_callback_exception(engine):
    trace=[];failure=ValueError('extension callback failed')
    class Broken(Extension):
        tags={'broken'}
        def parse(self,parser):
            token=next(parser.stream)
            return nodes.Output([self.call_method('_fail')]).set_lineno(token.lineno)
        def _fail(self):trace.append('callback');raise failure
    try:generate(engine,'{% broken %}',{},jinja2.Environment(extensions=[Broken]))
    except ValueError as error:return Result({'same_instance':error is failure,'message':str(error),'trace':trace},{})
    raise AssertionError('expected extension callback failure')


def extension_filter_stream(engine):
    class Replace(Extension):
        def filter_stream(self,stream):
            for token in stream:
                if token.type=='data':token=Token(token.lineno,token.type,token.value.replace('ORIGINAL_TOKEN','replacement'))
                yield token
    return observed(generate(engine,'ORIGINAL_TOKEN',{},jinja2.Environment(extensions=[Replace])))


def extension_scopes(engine):
    class Echo(Extension):
        tags={'echoexpr'}
        def parse(self,parser):
            token=next(parser.stream)
            return nodes.Output([parser.parse_expression()]).set_lineno(token.lineno)
    source=('{% set n=7 %}{% for x in [1,2] %}{% set n=x %}{% echoexpr n %}{% endfor %}|'
            '{% echoexpr n %}|{% macro m(x=4) %}{% echoexpr x+1 %}{% endmacro %}{{ m() }}|'
            '{% with x=3 %}{% if x>2 %}{% echoexpr x %}{% endif %}{% endwith %}|'
            '{% autoescape true %}{% echoexpr "<" %}{% endautoescape %}')
    return observed(generate(engine,source,{},jinja2.Environment(extensions=[Echo])))


def extension_loader_inheritance(engine):
    trace=[]
    class Echo(Extension):
        tags={'echoexpr'}
        def parse(self,parser):
            token=next(parser.stream);trace.append('parse')
            return nodes.Output([parser.parse_expression()]).set_lineno(token.lineno)
    class Loader(jinja2.DictLoader):
        def get_source(self,environment,name):
            trace.append(name)
            return super().get_source(environment,name)
    loader=Loader({'fragment':'{% extends "base" %}{% block body %}{% echoexpr value %}{% endblock %}',
                   'base':'A{% block body %}base{% endblock %}Z'})
    return observed(generate(engine,'[{% include "fragment" %}]',{'value':'X'},jinja2.Environment(extensions=[Echo],loader=loader)),trace)


def extension_preprocess(engine):
    class Replace(Extension):
        def preprocess(self,source,name,filename=None):return source.replace('ORIGINAL_TOKEN','replacement')
    return observed(generate(engine,'ORIGINAL_TOKEN',{},jinja2.Environment(extensions=[Replace])))


def extension_preprocess_plain_trace(engine):
    trace=[]
    class Replace(Extension):
        def preprocess(self,source,name,filename=None):
            trace.append(['ORIGINAL_TOKEN' in source,name,filename])
            return source.replace('ORIGINAL_TOKEN','replacement')
    return observed(generate(engine,'ORIGINAL_TOKEN',{},jinja2.Environment(extensions=[Replace])),trace)


def extension_multiple_preprocess(engine):
    class Replace(Extension):
        def preprocess(self,source,name,filename=None):return source.replace('ORIGINAL_TOKEN','replacement')
    class Finish(Extension):
        def preprocess(self,source,name,filename=None):return source.replace('replacement','finished')
    return observed(generate(engine,'ORIGINAL_TOKEN',{},jinja2.Environment(extensions=[Replace,Finish])))


def extension_preprocess_priorities(engine):
    class Finish(Extension):
        priority=200
        def preprocess(self,source,name,filename=None):return source.replace('replacement','finished')
    class Replace(Extension):
        priority=10
        def preprocess(self,source,name,filename=None):return source.replace('ORIGINAL_TOKEN','replacement')
    return observed(generate(engine,'ORIGINAL_TOKEN',{},jinja2.Environment(extensions=[Finish,Replace])))


def extension_hook_exception(engine, *, token_stream=False):
    trace=[];failure=ValueError('extension hook failed')
    class Broken(Extension):
        def preprocess(self,source,name,filename=None):
            if not token_stream:trace.append('preprocess');raise failure
            return source
        def filter_stream(self,stream):
            if token_stream:trace.append('filter_stream');raise failure
            return stream
    try:generate(engine,'hello',{},jinja2.Environment(extensions=[Broken]))
    except ValueError as error:return Result({'same_instance':error is failure,'message':str(error),'trace':trace},{})
    raise AssertionError('expected extension hook failure')


PROBES={
 'extension_expression':Probe(extension_expression,{'text':'X'}),
 'extension_method':Probe(extension_method,{'text':'<X>','trace':['X']}),
 'extension_assignment':Probe(extension_assignment,{'text':'7'}),
 'extension_parse_exception':Probe(extension_parse_exception,{'same_instance':True,'message':'extension parser failed','trace':['parse']},True),
 'extension_callback_exception':Probe(extension_callback_exception,{'same_instance':True,'message':'extension callback failed','trace':['callback']},True),
 'extension_filter_stream':Probe(extension_filter_stream,{'text':'replacement'}),
 'extension_scopes':Probe(extension_scopes,{'text':'12|7|5|3|<'}),
 'extension_loader_inheritance':Probe(extension_loader_inheritance,{'text':'[AXZ]','trace':['fragment','parse','base']}),
 'extension_preprocess':Probe(extension_preprocess,{'text':'replacement'}),
 'extension_preprocess_plain_trace':Probe(extension_preprocess_plain_trace,
     {'text':'replacement','trace':[[True,None,None]]+[[False,None,None]]*6}),
 'extension_multiple_preprocess':Probe(extension_multiple_preprocess,{'text':'finished'}),
 'extension_preprocess_priorities':Probe(extension_preprocess_priorities,{'text':'finished'}),
 'extension_preprocess_exception':Probe(extension_hook_exception,{'same_instance':True,'message':'extension hook failed','trace':['preprocess']},True),
 'extension_filter_stream_exception':Probe(lambda engine:extension_hook_exception(engine,token_stream=True),{'same_instance':True,'message':'extension hook failed','trace':['filter_stream']},True),
}
