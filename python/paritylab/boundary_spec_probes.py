"""Curated multi-input boundaries rendered together, preserving full DOCX checks."""
from .protocol_base import Probe,generate,observed

UNICODE=['','abc','ABC','straße','ß','ẞ','İ','ı','Σ','σ','ς','ΟΣ','ΟΣΑ','CAFÉ','café','αβ','αΒ','a1B','你好','🐍']
UPPER=['','ABC','ABC','STRASSE','SS','ẞ','İ','I','Σ','Σ','Σ','ΟΣ','ΟΣΑ','CAFÉ','CAFÉ','ΑΒ','ΑΒ','A1B','你好','🐍']
LOWER=['','abc','abc','straße','ß','ß','i\u0307','ı','σ','σ','ς','ος','οσα','café','café','αβ','αβ','a1b','你好','🐍']

def sequence(engine,expression,values):
    source='{% for value in values %}{{ '+expression+' }}{% if not loop.last %}|{% endif %}{% endfor %}'
    return observed(generate(engine,source,{'values':values}))

def unicode_upper(engine):return sequence(engine,'value|upper',UNICODE)
def unicode_lower(engine):return sequence(engine,'value|lower',UNICODE)
def integer_boundaries(engine):
    values=[None,False,True,0,-1,10**100,0.0,float('nan'),float('inf'),'1',[],{}]
    return sequence(engine,'value is integer',values)

PROBES={
 'boundary_unicode_upper':Probe(unicode_upper,{'text':'|'.join(UPPER)}),
 'boundary_unicode_lower':Probe(unicode_lower,{'text':'|'.join(LOWER)}),
 'boundary_integer_types':Probe(integer_boundaries,{'text':'False|False|False|True|True|True|False|False|False|False|False|False'}),
}


def markup_case(operation):
    def execute(engine):
        import jinja2
        from markupsafe import Markup
        source='{{ marked|'+operation+' is escaped }}|{{ plain|'+operation+' is escaped }}'
        return observed(generate(engine,source,{'marked':Markup('X'),'plain':'X'},jinja2.Environment(autoescape=True)))
    return execute


def default_boundaries(engine):
    import jinja2
    source="{{ missing|default('A') }}|{{ nonevalue|default('B') }}|{{ falsevalue|default('C') }}|{{ falsevalue|default('D',true) }}"
    return observed(generate(engine,source,{'nonevalue':None,'falsevalue':False},jinja2.Environment(undefined=jinja2.StrictUndefined)))

PROBES['boundary_upper_markup']=Probe(markup_case('upper'),{'text':'True|False'})
PROBES['boundary_lower_markup']=Probe(markup_case('lower'),{'text':'True|False'})
PROBES['boundary_default_undefined_falsey']=Probe(default_boundaries,{'text':'A|None|False|D'})
