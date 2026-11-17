"""Executable compatibility contracts for Python protocols and document state.

Each reference result has an independent expected observation. Matching setup
failures are errors, not evidence. Protocol timings are not speedup benchmarks.
"""
from __future__ import annotations

import io
import zipfile
from collections.abc import Mapping

import jinja2
from docx import Document
from docx.document import Document as DocumentType
from lxml import etree
from .protocol_base import Probe, Result, template, generate, observed


class CallbackError(Exception):
    pass


def object_access(engine):
    trace=[]
    class Account:
        @property
        def balance(self):trace.append('balance');return 7
        def label(self,prefix=''):trace.append('label:'+prefix);return prefix+'account'
    blob=generate(engine,"{{ account.balance }}|{{ account.label(prefix='my-') }}",{'account':Account()})
    return observed(blob,trace)


def method_item_fallback(engine):
    trace=[]
    def callback(value):trace.append(value);return value+1
    return observed(generate(engine,'{{ mapping.action(2) }}',{'mapping':{'action':callback}}),trace)


def callback_identity(engine):
    marker=object();trace=[]
    def identity(value):trace.append(value is marker);return 'same' if value is marker else 'copied'
    return observed(generate(engine,'{{ identity(marker) }}',{'identity':identity,'marker':marker}),trace)


def callback_error(engine):
    trace=[];error=CallbackError('business failure')
    def fail():trace.append('fail');raise error
    def after():trace.append('after');return 'unexpected'
    try:generate(engine,'{{ fail() }}{{ after() }}',{'fail':fail,'after':after})
    except CallbackError as actual:
        return Result({'exception':type(actual).__module__+'.'+type(actual).__name__,
                       'message':str(actual),'same_instance':actual is error,'trace':trace},{})
    raise AssertionError('expected callback exception was not raised')


def live_mapping(engine):
    values={'value':'before'};trace=[]
    def change():values['value']='after';trace.append('change');return ''
    return observed(generate(engine,'{{ values.value }}{{ change() }}{{ values.value }}',
                             {'values':values,'change':change}),trace,final=values['value'])


def list_mutation(engine):
    values=['A']
    blob=generate(engine,"{{ values.append('B') }}|{{ values|join(',') }}",{'values':values})
    return observed(blob,final=values)


def iterator_order(engine):
    trace=[]
    def values():
        for value in range(3):trace.append('yield:'+str(value));yield value
    def tick():trace.append('tick');return ''
    blob=generate(engine,'{% for value in values %}{{ value }}{{ tick() }}{% endfor %}',
                  {'values':values(),'tick':tick})
    return observed(blob,trace)


def generator_loop_metadata(engine):
    trace=[]
    def values():
        for value in range(3):trace.append('yield:'+str(value));yield value
    def emit(value):trace.append('emit:'+str(value));return value
    def tick():trace.append('tick');return ''
    blob=generate(engine,"{% for value in values %}{{ emit(value) }}{% if not loop.last %},{% endif %}{{ tick() }}{% endfor %}",
                  {'values':values(),'emit':emit,'tick':tick})
    return observed(blob,trace)


def iterator_error(engine):
    trace=[];error=CallbackError('iterator failure')
    def values():
        trace.append('yield:1');yield 1
        trace.append('raise');raise error
    def tick():trace.append('tick');return ''
    try:generate(engine,'{% for value in values %}{{ value }}{{ tick() }}{% endfor %}',
                 {'values':values(),'tick':tick})
    except CallbackError as actual:
        return Result({'message':str(actual),'same_instance':actual is error,'trace':trace},{})
    raise AssertionError('expected iterator exception was not raised')


def list_grows_during_loop(engine):
    values=[1]
    def grow(value):
        if value==1:values.append(2)
        return ''
    return observed(generate(engine,'{% for value in values %}{{ value }}{{ grow(value) }}{% endfor %}',
                             {'values':values,'grow':grow}),final=values)


def lookup_priority(engine):
    trace=[]
    class Dual:
        @property
        def name(self):trace.append('attribute');return 'attribute'
        def __getitem__(self,key):trace.append('item:'+key);return 'item'
    return observed(generate(engine,"{{ value.name }}|{{ value['name'] }}",{'value':Dual()}),trace)


def pass_context(engine):
    environment=jinja2.Environment();trace=[]
    @jinja2.pass_context
    def prefix(context,value):
        trace.append(isinstance(context,jinja2.runtime.Context))
        return context['prefix']+context['local']+value
    environment.filters['prefix']=prefix
    blob=generate(engine,"{% set local = 'L' %}{{ value|prefix }}",{'value':'X','prefix':'P'},environment)
    return observed(blob,trace)


def pass_environment(engine):
    environment=jinja2.Environment();trace=[]
    @jinja2.pass_environment
    def check(actual,value):trace.append(actual is environment);return value
    environment.filters['check']=check
    return observed(generate(engine,'{{ value|check }}',{'value':'X'},environment),trace)


def pass_eval_context(engine):
    environment=jinja2.Environment(autoescape=True);trace=[]
    @jinja2.pass_eval_context
    def check(context,value):trace.append(context.autoescape);return value
    environment.filters['check']=check
    return observed(generate(engine,'{{ value|check }}',{'value':'<x>'},environment),trace)


def filter_kwargs(engine):
    environment=jinja2.Environment();trace=[]
    def surround(value,left='[',right=']'):trace.append([left,right]);return left+value+right
    environment.filters['surround']=surround
    return observed(generate(engine,"{{ value|surround(right=')',left='(') }}",{'value':'X'},environment),trace)


def builtin_override(engine):
    environment=jinja2.Environment();environment.filters['upper']=lambda value:'override:'+value
    return observed(generate(engine,'{{ value|upper }}',{'value':'X'},environment))


def custom_test(engine):
    environment=jinja2.Environment();environment.tests['positive']=lambda value:value>0
    return observed(generate(engine,"{% if value is positive %}yes{% else %}no{% endif %}",{'value':1},environment))


def finalize(engine):
    trace=[]
    def finalizer(value):trace.append(value);return 'EMPTY' if value is None else value
    return observed(generate(engine,'{{ value }}',{'value':None},jinja2.Environment(finalize=finalizer)),trace)


def finalize_context(engine):
    trace=[]
    @jinja2.pass_context
    def finalizer(context,value):
        trace.append(isinstance(context,jinja2.runtime.Context))
        return context['local']+value
    return observed(generate(engine,"{% set local='L' %}{{ value }}",{'value':'X'},jinja2.Environment(finalize=finalizer)),trace)


def loader(engine):
    environment=jinja2.Environment(loader=jinja2.DictLoader({'fragment':'included {{ value }}'}))
    return observed(generate(engine,"{% include 'fragment' %}",{'value':'X'},environment))


def strict_undefined(engine):
    try:generate(engine,'{{ missing }}',{},jinja2.Environment(undefined=jinja2.StrictUndefined))
    except jinja2.UndefinedError as error:
        return Result({'exception':type(error).__module__+'.'+type(error).__name__,'message':str(error)},{})
    raise AssertionError('StrictUndefined did not raise')


def undefined_default(engine):
    return observed(generate(engine,"{{ missing|default('fallback') }}|{{ missing is defined }}",{}))


def nested_render(engine):
    trace=[]
    def nested():
        trace.append('enter')
        text=Document(io.BytesIO(generate(engine,'nested {{ value }}',{'value':'X'}))).paragraphs[0].text
        trace.append('leave');return text
    return observed(generate(engine,'outer {{ nested() }}',{'nested':nested}),trace)


def per_part_snapshot(engine):
    context={'value':'before'};trace=[]
    def change():context['value']='after';trace.append('change');return ''
    context['change']=change
    blob=generate(engine,'',context,source=template('{{ change() }}{{ value }}',header='{{ value }}'))
    return observed(blob,trace,header=Document(io.BytesIO(blob)).sections[0].header.paragraphs[0].text)


def mapping_snapshot_counts(engine):
    trace=[]
    class Context(Mapping):
        def __iter__(self):trace.append('snapshot');return iter(['value'])
        def __len__(self):return 1
        def __getitem__(self,key):
            if key=='value':return 'X'
            raise KeyError(key)
    return observed(generate(engine,'{{ value }}',Context()),trace)


def metadata_template():
    document=Document();document.add_paragraph('plain');document.core_properties.title='{{ value }}'
    output=io.BytesIO();document.save(output)
    return output.getvalue()


def metadata_limit(engine):
    try:generate(engine,'',{'value':'X'*256},source=metadata_template())
    except ValueError as error:return Result({'exception':'builtins.ValueError','message':str(error)},{})
    raise AssertionError('metadata length limit was not enforced')


def metadata_nul(engine):
    try:generate(engine,'',{'value':'a\x00b'},source=metadata_template())
    except ValueError as error:return Result({'exception':'builtins.ValueError','message':str(error)},{})
    raise AssertionError('NUL in metadata was accepted')


def metadata_namespace_alias(engine):
    output=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(metadata_template())) as source,zipfile.ZipFile(output,'w') as target:
        for info in source.infolist():
            data=source.read(info.filename)
            if info.filename=='docProps/core.xml':data=data.replace(b'dc:',b'alternate:').replace(b'xmlns:dc=',b'xmlns:alternate=')
            target.writestr(info,data)
    blob=generate(engine,'',{'value':'X'},source=output.getvalue())
    return observed(blob,title=Document(io.BytesIO(blob)).core_properties.title)


def document_types(engine):
    document=engine.DocxTemplate(io.BytesIO(template('plain'))).get_docx()
    return Result({'python_docx_document':isinstance(document,DocumentType),
                   'lxml_element':isinstance(getattr(document,'_element',None),etree._Element)}, {})


def package_index_order(engine):
    output=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(template('{{ value }}'))) as source,zipfile.ZipFile(output,'w') as target:
        for info in source.infolist():
            data=source.read(info.filename)
            if info.filename=='[Content_Types].xml':
                root=etree.fromstring(data)
                root[:]=list(reversed(root))
                data=etree.tostring(root,xml_declaration=True,encoding='UTF-8',standalone=True)
            target.writestr(info,data)
    return observed(generate(engine,'',{'value':'X'},source=output.getvalue()))


def namespace_state(engine):
    return observed(generate(engine,'{% set ns = namespace(found=false) %}{% set ns.found = true %}{{ ns.found }}',{}))


def retained_references(engine):
    document=engine.DocxTemplate(io.BytesIO(template('Hello {{ value }}')))
    live=document.get_docx();paragraph=live.paragraphs[0];run=paragraph.runs[0];run.bold=True
    document.render({'value':'X'})
    output=io.BytesIO();document.save(output)
    blob=output.getvalue();current=Document(io.BytesIO(blob))
    return observed(blob,retained_text=paragraph.text,retained_bold=run.bold,
                    output_bold=current.paragraphs[0].runs[0].bold)


def get_docx_reload(engine):
    document=engine.DocxTemplate(io.BytesIO(template('Hello {{ value }}')))
    document.render({'value':'X'})
    before=io.BytesIO();document.save(before)
    first=document.get_docx()
    same_document=document.get_docx() is first
    after=io.BytesIO();document.save(after)
    return Result({'before':Document(io.BytesIO(before.getvalue())).paragraphs[0].text,
                   'after':Document(io.BytesIO(after.getvalue())).paragraphs[0].text,
                   'is_rendered':document.is_rendered,'is_saved':document.is_saved,'same_document':same_document},
                  {'before.docx':before.getvalue(),'after.docx':after.getvalue()})


def truthiness_error(engine):
    trace=[];error=CallbackError('truthiness failure')
    class Broken:
        def __bool__(self):trace.append('bool');raise error
    def after():trace.append('after');return ''
    try:generate(engine,'{% if value %}yes{% else %}{{ after() }}{% endif %}',{'value':Broken(),'after':after})
    except CallbackError as actual:
        return Result({'message':str(actual),'same_instance':actual is error,'trace':trace},{})
    raise AssertionError('truthiness exception was not raised')


def environment_subclass(engine):
    class CustomEnvironment(jinja2.Environment):
        def getattr(self,obj,attribute):
            if attribute=='name':return 'custom lookup'
            return super().getattr(obj,attribute)
    return observed(generate(engine,'{{ value.name }}',{'value':object()},CustomEnvironment()))


def undefined_subclass(engine):
    class CustomUndefined(jinja2.Undefined):
        def __str__(self):return 'custom undefined'
    return observed(generate(engine,'{{ missing }}',{},jinja2.Environment(undefined=CustomUndefined)))


def extension_ast(engine):
    from jinja2 import nodes
    from jinja2.ext import Extension
    class EchoExtension(Extension):
        tags={'echo'}
        def parse(self,parser):
            token=next(parser.stream)
            return nodes.Output([nodes.Const('extension output')]).set_lineno(token.lineno)
    return observed(generate(engine,'{% echo %}',{},jinja2.Environment(extensions=[EchoExtension])))


def sandbox_forbidden(engine):
    from jinja2.sandbox import SandboxedEnvironment,SecurityError,unsafe
    trace=[]
    @unsafe
    def forbidden():trace.append('called');return 'forbidden'
    try:generate(engine,'{{ forbidden() }}',{'forbidden':forbidden},SandboxedEnvironment())
    except SecurityError:return Result({'exception':'jinja2.exceptions.SecurityError','trace':trace},{})
    raise AssertionError('sandbox allowed an unsafe call')


def async_callback(engine):
    async def callback():return 'async result'
    return observed(generate(engine,'{{ callback() }}',{'callback':callback},jinja2.Environment(enable_async=True)))


def syntax_extension_disabled(engine):
    values=[]
    try:generate(engine,"{% do values.append('unexpected') %}",{'values':values})
    except jinja2.TemplateSyntaxError as error:
        return Result({'exception':'jinja2.exceptions.TemplateSyntaxError','message':error.message,'values':values},{})
    raise AssertionError('disabled do extension was accepted')


PROBES={
    'object_access':Probe(object_access,{'text':'7|my-account','trace':['balance','label:my-']}),
    'method_item_fallback':Probe(method_item_fallback,{'text':'3','trace':[2]}),
    'callback_identity':Probe(callback_identity,{'text':'same','trace':[True]}),
    'callback_error':Probe(callback_error,{'exception':__name__+'.CallbackError','message':'business failure','same_instance':True,'trace':['fail']},True),
    'live_mapping':Probe(live_mapping,{'text':'beforeafter','trace':['change'],'final':'after'}),
    'list_mutation':Probe(list_mutation,{'text':'None|A,B','final':['A','B']}),
    'iterator_order':Probe(iterator_order,{'text':'012','trace':['yield:0','tick','yield:1','tick','yield:2','tick']}),
    'generator_loop_metadata':Probe(generator_loop_metadata,{'text':'0,1,2','trace':['yield:0','emit:0','yield:1','tick','emit:1','yield:2','tick','emit:2','tick']}),
    'iterator_error':Probe(iterator_error,{'message':'iterator failure','same_instance':True,'trace':['yield:1','tick','raise']},True),
    'list_grows_during_loop':Probe(list_grows_during_loop,{'text':'12','final':[1,2]}),
    'lookup_priority':Probe(lookup_priority,{'text':'attribute|item','trace':['attribute','item:name']}),
    'pass_context':Probe(pass_context,{'text':'PLX','trace':[True]}),
    'pass_environment':Probe(pass_environment,{'text':'X','trace':[True]}),
    'pass_eval_context':Probe(pass_eval_context,{'text':'<x>','trace':[True]}),
    'filter_kwargs':Probe(filter_kwargs,{'text':'(X)','trace':[['(',')']]}),
    'builtin_override':Probe(builtin_override,{'text':'override:X'}),
    'custom_test':Probe(custom_test,{'text':'yes'}),
    'finalize':Probe(finalize,{'text':'EMPTY','trace':[None]}),
    'finalize_context':Probe(finalize_context,{'text':'LX','trace':[True]}),
    'loader':Probe(loader,{'text':'included X'}),
    'strict_undefined':Probe(strict_undefined,{'exception':'jinja2.exceptions.UndefinedError','message':"'missing' is undefined"},True),
    'undefined_default':Probe(undefined_default,{'text':'fallback|False'}),
    'nested_render':Probe(nested_render,{'text':'outer nested X','trace':['enter','leave']}),
    'per_part_snapshot':Probe(per_part_snapshot,{'text':'before','trace':['change'],'header':'after'}),
    'mapping_snapshot_counts':Probe(mapping_snapshot_counts,{'text':'X','trace':['snapshot']*7}),
    'metadata_limit':Probe(metadata_limit,{'exception':'builtins.ValueError','message':"exceeded 255 char limit for property, got:\n\n'"+'X'*256+"'"},True),
    'metadata_nul':Probe(metadata_nul,{'exception':'builtins.ValueError','message':'All strings must be XML compatible: Unicode or ASCII, no NULL bytes or control characters'},True),
    'metadata_namespace_alias':Probe(metadata_namespace_alias,{'text':'plain','title':'X'}),
    'package_index_order':Probe(package_index_order,{'text':'X'}),
    'namespace_state':Probe(namespace_state,{'text':'True'}),
    'document_types':Probe(document_types,{'python_docx_document':True,'lxml_element':True},True),
    'retained_references':Probe(retained_references,{'text':'Hello X','retained_text':'Hello {{ value }}','retained_bold':True,'output_bold':True}),
    'get_docx_reload':Probe(get_docx_reload,{'before':'Hello X','after':'Hello {{ value }}','is_rendered':False,'is_saved':True,'same_document':True}),
    'truthiness_error':Probe(truthiness_error,{'message':'truthiness failure','same_instance':True,'trace':['bool']},True),
    'environment_subclass':Probe(environment_subclass,{'text':'custom lookup'}),
    'undefined_subclass':Probe(undefined_subclass,{'text':'custom undefined'}),
    'extension_ast':Probe(extension_ast,{'text':'extension output'}),
    'sandbox_forbidden':Probe(sandbox_forbidden,{'exception':'jinja2.exceptions.SecurityError','trace':[]},True),
    'async_callback':Probe(async_callback,{'text':'async result'}),
    'syntax_extension_disabled':Probe(syntax_extension_disabled,{'exception':'jinja2.exceptions.TemplateSyntaxError','message':"Encountered unknown tag 'do'.",'values':[]},True),
}


def execute(name,engine,validate_reference=False):
    probe=PROBES[name];result=probe.execute(engine)
    if validate_reference and result.observation!=probe.expected:
        raise AssertionError(f'reference contract changed for {name}: {result.observation!r} != {probe.expected!r}')
    if not result.outputs and not probe.assertion_only:
        raise AssertionError('protocol produced no DOCX without an assertion-only contract')
    return result


from .opc_probes import PROBES as OPC_PROBES
PROBES.update(OPC_PROBES)
from .iteration_probes import PROBES as ITERATION_PROBES
PROBES.update(ITERATION_PROBES)
from .macro_probes import PROBES as MACRO_PROBES
PROBES.update(MACRO_PROBES)
from .operator_probes import PROBES as OPERATOR_PROBES
PROBES.update(OPERATOR_PROBES)
from .image_probes import PROBES as IMAGE_PROBES
PROBES.update(IMAGE_PROBES)
from .analysis_probes import PROBES as ANALYSIS_PROBES
PROBES.update(ANALYSIS_PROBES)
from .compilation_probes import PROBES as COMPILATION_PROBES
PROBES.update(COMPILATION_PROBES)
from .finalizer_probes import PROBES as FINALIZER_PROBES
PROBES.update(FINALIZER_PROBES)
from .hyperlink_probes import PROBES as HYPERLINK_PROBES
PROBES.update(HYPERLINK_PROBES)
from .rich_value_probes import PROBES as RICH_VALUE_PROBES
PROBES.update(RICH_VALUE_PROBES)
from .document_model_probes import PROBES as DOCUMENT_MODEL_PROBES
PROBES.update(DOCUMENT_MODEL_PROBES)
from .builtin_spec_probes import PROBES as BUILTIN_SPEC_PROBES
PROBES.update(BUILTIN_SPEC_PROBES)
from .builtin_expansion_probes import PROBES as BUILTIN_EXPANSION_PROBES
PROBES.update(BUILTIN_EXPANSION_PROBES)
from .api_spec_probes import PROBES as API_SPEC_PROBES
PROBES.update(API_SPEC_PROBES)
from .boundary_spec_probes import PROBES as BOUNDARY_SPEC_PROBES
PROBES.update(BOUNDARY_SPEC_PROBES)
from .word_tag_spec_probes import PROBES as WORD_TAG_SPEC_PROBES
PROBES.update(WORD_TAG_SPEC_PROBES)
from .language_spec_probes import PROBES as LANGUAGE_SPEC_PROBES
PROBES.update(LANGUAGE_SPEC_PROBES)
from .output_gap_probes import PROBES as OUTPUT_GAP_PROBES
PROBES.update(OUTPUT_GAP_PROBES)
from .iteration_spec_probes import PROBES as ITERATION_SPEC_PROBES
PROBES.update(ITERATION_SPEC_PROBES)
from .native_predicate_probes import PROBES as NATIVE_PREDICATE_PROBES
PROBES.update(NATIVE_PREDICATE_PROBES)
from .predicate_global_probes import PROBES as PREDICATE_GLOBAL_PROBES
PROBES.update(PREDICATE_GLOBAL_PROBES)
from .extended_predicate_probes import PROBES as EXTENDED_PREDICATE_PROBES
PROBES.update(EXTENDED_PREDICATE_PROBES)
from .api_surface_probes import PROBES as API_SURFACE_PROBES
PROBES.update(API_SURFACE_PROBES)
from .composer_probes import PROBES as COMPOSER_PROBES
PROBES.update(COMPOSER_PROBES)
from .async_spec_probes import PROBES as ASYNC_SPEC_PROBES
PROBES.update(ASYNC_SPEC_PROBES)
from .extension_spec_probes import PROBES as EXTENSION_SPEC_PROBES
PROBES.update(EXTENSION_SPEC_PROBES)
from .delimiter_probes import PROBES as DELIMITER_PROBES
PROBES.update(DELIMITER_PROBES)
from .embedded_probes import PROBES as EMBEDDED_PROBES
PROBES.update(EMBEDDED_PROBES)
from .concat_probes import PROBES as CONCAT_PROBES
PROBES.update(CONCAT_PROBES)

from .context_lifecycle_probes import PROBES as CONTEXT_LIFECYCLE_PROBES
PROBES.update(CONTEXT_LIFECYCLE_PROBES)

from .runtime_error_probes import PROBES as RUNTIME_ERROR_PROBES
PROBES.update(RUNTIME_ERROR_PROBES)

from .include_scope_probes import PROBES as INCLUDE_SCOPE_PROBES
PROBES.update(INCLUDE_SCOPE_PROBES)

from .live_save_probes import PROBES as LIVE_SAVE_PROBES
PROBES.update(LIVE_SAVE_PROBES)

from .context_creation_probes import PROBES as CONTEXT_CREATION_PROBES
PROBES.update(CONTEXT_CREATION_PROBES)
from .context_helpers_probes import PROBES as CONTEXT_HELPER_PROBES
PROBES.update(CONTEXT_HELPER_PROBES)
from .zip_lifecycle_probes import PROBES as ZIP_LIFECYCLE_PROBES
PROBES.update(ZIP_LIFECYCLE_PROBES)
from .sandbox_call_probes import PROBES as SANDBOX_CALL_PROBES
PROBES.update(SANDBOX_CALL_PROBES)
from .sandbox_operator_probes import PROBES as SANDBOX_OPERATOR_PROBES
PROBES.update(SANDBOX_OPERATOR_PROBES)
from .call_exception_probes import PROBES as CALL_EXCEPTION_PROBES
PROBES.update(CALL_EXCEPTION_PROBES)
from .call_policy_probes import PROBES as CALL_POLICY_PROBES
PROBES.update(CALL_POLICY_PROBES)
from .dynamic_keyword_probes import PROBES as DYNAMIC_KEYWORD_PROBES
PROBES.update(DYNAMIC_KEYWORD_PROBES)
from .builtin_partition_probes import PROBES as BUILTIN_PARTITION_PROBES
PROBES.update(BUILTIN_PARTITION_PROBES)
from .internal_error_probes import PROBES as INTERNAL_ERROR_PROBES
PROBES.update(INTERNAL_ERROR_PROBES)
from .tag_partition_probes import PROBES as TAG_PARTITION_PROBES
PROBES.update(TAG_PARTITION_PROBES)
from .api_partition_probes import PROBES as API_PARTITION_PROBES
PROBES.update(API_PARTITION_PROBES)
from .scenario_partition_probes import PROBES as SCENARIO_PARTITION_PROBES
PROBES.update(SCENARIO_PARTITION_PROBES)
from .language_partition_probes import PROBES as LANGUAGE_PARTITION_PROBES
PROBES.update(LANGUAGE_PARTITION_PROBES)
