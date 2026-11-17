"""Undeclared-variable analysis must preserve the current document lifecycle."""
import io
import jinja2
from .protocol_base import Probe, Result, template

def undeclared_analysis(engine):
    document=engine.DocxTemplate(io.BytesIO(template('Hello {{ name }} {{ missing }}',header='{{ header }}')))
    before=sorted(document.get_undeclared_template_variables())
    initial_flags=[document.is_rendered,document.is_saved]
    document.render({'name':'X','header':'H'})
    first=io.BytesIO();document.save(first)
    remaining=sorted(document.get_undeclared_template_variables(context={'name':'X','header':'H'}))
    flags=[document.is_rendered,document.is_saved]
    second=io.BytesIO();document.save(second)
    return Result({'before':before,'remaining':remaining,'initial_flags':initial_flags,'flags':flags},
                  {'before.docx':first.getvalue(),'after.docx':second.getvalue()})

def analysis_custom_environment(engine):
    trace=[]
    class Environment(jinja2.Environment):
        def parse(self,source,*args,**kwargs):trace.append('parse');return super().parse(source,*args,**kwargs)
    environment=Environment(variable_start_string='[[',variable_end_string=']]')
    environment.globals['provided']='global'
    document=engine.DocxTemplate(io.BytesIO(template('[[ name ]] [[ provided ]]',header='[[ head ]]')))
    values=document.get_undeclared_template_variables(jinja_env=environment,context={'head':'H'})
    return Result({'variables':sorted(values),'trace':trace},{})


def analysis_relationship_callbacks(engine):
    import importlib
    from types import SimpleNamespace
    from unittest.mock import patch
    module=importlib.import_module('docxtpl.template' if engine.__name__=='docxtpl' else 'docxtpl_native')
    trace=[];body=object();header=object();footer=object();tree=object()
    class Part:
        def __init__(self,label):self.label=label
        @property
        def blob(self):trace.append('blob:'+self.label);return self.label.encode()
    class Rel:
        def __init__(self,kind,label):self.reltype=kind;self.target_part=Part(label)
    class Rels:
        def items(self):trace.append('items');return iter([('h',Rel('header','h')),('f',Rel('footer','f'))])
    class Subject:
        template_file='source';HEADER_URI='header';FOOTER_URI='footer'
        def xml_to_string(self,value):
            label='B' if value is body else 'H' if value is header else 'F'
            trace.append('serialize:'+label);return label
        def patch_xml(self,value):trace.append('patch:'+value);return '['+value+']'
    def document(value):trace.append('document');return SimpleNamespace(_element=SimpleNamespace(body=body),_part=SimpleNamespace(rels=Rels()))
    def parse_xml(value):trace.append('parse_xml:'+value.decode());return header if value==b'h' else footer
    class Environment:
        def __bool__(self):trace.append('env_bool');return True
        def parse(self,value):trace.append('parse:'+value);return tree
    class Meta:
        def find_undeclared_variables(self,value):assert value is tree;trace.append('meta');return {'one','two'}
    class Context:
        def keys(self):trace.append('keys');return ['two']
    with patch.object(module,'Document',document,create=True),patch.object(module,'parse_xml',parse_xml,create=True),patch.object(module,'meta',Meta(),create=True):
        result=engine.DocxTemplate.get_undeclared_template_variables(Subject(),Environment(),Context())
    return Result({'variables':sorted(result),'trace':trace},{})

PROBES={
    'undeclared_analysis':Probe(undeclared_analysis,{'before':['header','missing','name'],'remaining':['missing'],
        'initial_flags':[False,False],'flags':[True,True]}),
    'analysis_custom_environment':Probe(analysis_custom_environment,{'variables':['name'],'trace':['parse']},True),
    'analysis_relationship_callbacks':Probe(analysis_relationship_callbacks,{'variables':['one'],'trace':[
        'document','serialize:B','patch:B','items','blob:h','blob:h','parse_xml:h','serialize:H','patch:H',
        'items','blob:f','blob:f','parse_xml:f','serialize:F','patch:F','env_bool','parse:[B][H][F]','meta','keys']},True),
}
