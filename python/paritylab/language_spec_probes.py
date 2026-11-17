"""Jinja grammar/scopes inside a property, with complete package comparisons."""
import io
import jinja2
from docx import Document
from .protocol_base import Probe,Result

CASES={
 'for_else_empty':('{% for v in values %}{{ v }}{% else %}empty{% endfor %}',{'values':[]},'empty',None),
 'for_filter':('{% for v in values if v is odd %}{{ v }}{% endfor %}',{'values':[1,2,3]},'13',None),
 'tuple_assignment':('{% set a,b=values %}{{ a }}/{{ b }}',{'values':['A','B']},'A/B',None),
 'capture':('{% set x %}{{ value }}{% endset %}{{ x }}',{'value':'X'},'X',None),
 'with_scope':("{% set x='outer' %}{% with x=value %}{{ x }}{% endwith %}{{ x }}",{'value':'inner'},'innerouter',None),
 'filter_block':('{% filter upper %}{{ value }}{% endfilter %}',{'value':'x'},'X',None),
 'macro_recursion':('{% macro m(n) %}{{ n }}{% if n>0 %}{{ m(n-1) }}{% endif %}{% endmacro %}{{ m(value) }}',{'value':2},'210',None),
 'caller':('{% macro m() %}[{{ caller() }}]{% endmacro %}{% call m() %}{{ value }}{% endcall %}',{'value':'X'},'[X]',None),
 'import':("{% import 'lib' as m %}{{ m.greet(value) }}",{'value':'X'},'X',{'lib':'{% macro greet(x) %}{{ x }}{% endmacro %}'}),
 'from_import':("{% from 'lib' import greet as alias %}{{ alias(value) }}",{'value':'X'},'X',{'lib':'{% macro greet(x) %}{{ x }}{% endmacro %}'}),
 'import_context':("{% import 'lib' as m with context %}{{ m.greet() }}",{'value':'X'},'X',{'lib':'{% macro greet() %}{{ value }}{% endmacro %}'}),
 'inheritance':("{% extends 'base' %}{% block body %}{{ value }}{% endblock %}",{'value':'X'},'AXC',{'base':'A{% block body %}B{% endblock %}C'}),
 'super':("{% extends 'base' %}{% block body %}{{ super() }}{{ value }}{% endblock %}",{'value':'X'},'ABXC',{'base':'A{% block body %}B{% endblock %}C'}),
 'do':("{% do values.append('X') %}{{ values|join(',') }}",{'values':[]},'X',None),
 'break':('{% for v in values %}{{ v }}{% if v==2 %}{% break %}{% endif %}{% endfor %}',{'values':[1,2,3]},'12',None),
 'continue':('{% for v in values %}{% if v==2 %}{% continue %}{% endif %}{{ v }}{% endfor %}',{'values':[1,2,3]},'13',None),
}

def execute(source,context,loader,extensions,globals=None):
    def probe(engine):
        import copy
        source_doc=Document();source_doc.add_paragraph('body');source_doc.core_properties.author=source
        buffer=io.BytesIO();source_doc.save(buffer)
        environment=jinja2.Environment(loader=jinja2.DictLoader(loader or {}),extensions=extensions)
        environment.globals.update(globals or {})
        document=engine.DocxTemplate(io.BytesIO(buffer.getvalue()));document.render(copy.deepcopy(context),jinja_env=environment)
        output=io.BytesIO();document.save(output);blob=output.getvalue()
        return Result({'author':Document(io.BytesIO(blob)).core_properties.author},{'document.docx':blob})
    return probe

CASES.update({
 'break_else':('{% for v in values %}{{ v }}{% break %}{% else %}E{% endfor %}',{'values':[1]},'1E',None),
 'continue_else':('{% for v in values %}{{ v }}{% continue %}{% else %}E{% endfor %}',{'values':[1]},'1E',None),
})
CASES.update({
 'for_filter_empty':('{% for v in values if v is odd %}{{ v }}{% else %}E{% endfor %}',{'values':[2,4]},'E',None),
 'for_filter_metadata':('{% for v in values if v is odd %}{{ loop.index }}/{{ loop.length }}/{{ v }};{% endfor %}',{'values':[1,2,3]},'1/2/1;2/2/3;',None),
 'nested_break_else':('{% for outer in values %}{{ outer }}:{% for inner in values %}{{ inner }}{% break %}{% else %}E{% endfor %};{% else %}O{% endfor %}',{'values':[1,2]},'1:1E;2:1E;',None),
 'continue_after_completed_iteration':('{% for v in values %}{% if v==2 %}{% continue %}{% endif %}{{ v }}{% else %}E{% endfor %}',{'values':[1,2]},'1',None),
 'break_after_completed_iteration':('{% for v in values %}{{ v }}{% if v==2 %}{% break %}{% endif %}{% else %}E{% endfor %}',{'values':[1,2]},'12',None),
 'namespace_assignment':('{% set ns=namespace(total=0) %}{% for v in values %}{% set ns.total=ns.total+v %}{% endfor %}{{ ns.total }}',{'values':[1,2,3]},'6',None),
 'recursive_loop':('{% for v in values recursive %}{{ v.name }}{% if v.children %}[{{ loop(v.children) }}]{% endif %}{% endfor %}',{'values':[{'name':'A','children':[{'name':'B','children':[]}]}]},'A[B]',None),
 'include_context':("{% include 'part' %}",{'value':'X'},'X',{'part':'{{ value }}'}),
 'include_without_context':("{% include 'part' without context %}",{'value':'X'},'U',{'part':"{{ value|default('U') }}"}),
 'include_ignore_missing':("A{% include 'absent' ignore missing %}B",{},'AB',{}),
 'include_candidates':("{% include ['absent','part'] %}",{'value':'X'},'X',{'part':'{{ value }}'}),
 'macro_defaults':('{% macro m(a,b=a) %}{{ a }}/{{ b }}{% endmacro %}{{ m(value) }}',{'value':'X'},'X/X',None),
 'loop_assignment_scope':("{% set value='O' %}{% for v in values %}{% set value=v %}{{ value }}{% endfor %}{{ value }}",{'values':['A','B']},'ABO',None),
 'conditional_expression':('{{ a if condition else b }}',{'a':'A','b':'B','condition':False},'B',None),
 'block_assignment_filter':('{% set result|upper %}{{ value }}{% endset %}{{ result }}',{'value':'x'},'X',None),
})
PROBES={}
for name,(source,context,expected,loader) in CASES.items():
    extensions=['jinja2.ext.do'] if name=='do' else ['jinja2.ext.loopcontrols'] if ('break' in name or 'continue' in name) else []
    PROBES['language_'+name]=Probe(execute(source,context,loader,extensions),{'author':expected})


@jinja2.pass_context
def context_marker(context):
    return context.get('prefix','U')

for label,marker,expected in [('with','with','P|X'),('without','without','U|U')]:
    PROBES['language_include_callback_'+label]=Probe(
        execute("{% include 'part' "+marker+" context %}",{'prefix':'P','value':'X'},
                {'part':"{{ marker() }}|{{ value|default('U') }}"},[],{'marker':context_marker}),
        {'author':expected})
