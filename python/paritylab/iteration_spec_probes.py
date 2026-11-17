"""Observable filtering order; eager and lazy implementations must differ here."""
import io
import jinja2
from docx import Document
from .protocol_base import Probe,Result


def filtered_context_scope(engine):
    trace=[];environment=jinja2.Environment()
    @jinja2.pass_context
    def selected(context,value):
        trace.append(['filter',value,context.get('value','missing'),'loop' in context])
        return value%2==1
    def emit(value):trace.append(['emit',value]);return value
    environment.tests['selected']=selected
    source='{% for value in values if value is selected %}{{ emit(value) }}:{{ loop.last }};{% endfor %}'
    doc=Document();doc.add_paragraph(source);buffer=io.BytesIO();doc.save(buffer)
    template=engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
    template.render({'values':[1,2,3],'value':'outer','emit':emit},jinja_env=environment)
    output=io.BytesIO();template.save(output);blob=output.getvalue()
    return Result({'text':Document(io.BytesIO(blob)).paragraphs[0].text,'trace':trace},{'document.docx':blob})


def filtered_order(early_break=False, suffix=''):
    def execute(engine):
        trace=[]
        def selected(value):
            trace.append('filter:'+str(value))
            return value%2==1
        def emit(value):
            trace.append('emit:'+str(value))
            return value
        environment=jinja2.Environment(extensions=['jinja2.ext.loopcontrols'] if early_break else [])
        environment.tests['selected']=selected
        source='{% for value in values if value is selected %}{{ emit(value) }}'+suffix+('{% break %}' if early_break else '')+'{% endfor %}'
        doc=Document();doc.add_paragraph(source);buffer=io.BytesIO();doc.save(buffer)
        template=engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
        context={'values':[1,2,3],'emit':emit}
        if suffix:context['loopy']='L'
        template.render(context,jinja_env=environment)
        output=io.BytesIO();template.save(output);blob=output.getvalue()
        return Result({'text':Document(io.BytesIO(blob)).paragraphs[0].text,'trace':trace},{'document.docx':blob})
    return execute


def filtered_else_order(values, control=''):
    def execute(engine):
        trace=[]
        def selected(value):trace.append('filter:'+str(value));return value%2==1
        def emit(value):trace.append('emit:'+str(value));return value
        environment=jinja2.Environment(extensions=['jinja2.ext.loopcontrols'] if control else [])
        environment.tests['selected']=selected
        source='{% for value in values if value is selected %}{{ emit(value) }}'+control+'{% else %}E{% endfor %}'
        doc=Document();doc.add_paragraph(source);buffer=io.BytesIO();doc.save(buffer)
        template=engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
        template.render({'values':values,'emit':emit},jinja_env=environment)
        output=io.BytesIO();template.save(output);blob=output.getvalue()
        return Result({'text':Document(io.BytesIO(blob)).paragraphs[0].text,'trace':trace},{'document.docx':blob})
    return execute


def filtered_else_nested(engine, *, break_from_else=False):
    trace=[]
    def selected(value):trace.append('filter:'+str(value));return value%2==1
    def emit(value):trace.append('emit:'+str(value));return value
    environment=jinja2.Environment(extensions=['jinja2.ext.loopcontrols'] if break_from_else else []);environment.tests['selected']=selected
    control='{% break %}' if break_from_else else ''
    source='{% for group in groups %}{% for value in group if value is selected %}{{ emit(value) }}{% else %}E'+control+'{% endfor %};{% endfor %}'
    doc=Document();doc.add_paragraph(source);buffer=io.BytesIO();doc.save(buffer)
    template=engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
    groups=[[2,4],[1]] if break_from_else else [[1,2],[2,4]]
    template.render({'groups':groups,'emit':emit},jinja_env=environment)
    output=io.BytesIO();template.save(output);blob=output.getvalue()
    return Result({'text':Document(io.BytesIO(blob)).paragraphs[0].text,'trace':trace},{'document.docx':blob})


def filtered_destructure(engine, *, break_with_else=False, one_shot=False):
    trace=[]
    class Pair:
        def __init__(self,number,letter):
            self.number=number;self.letter=letter;self.iterator=iter((number,letter))
        def __iter__(self):
            trace.append('unpack:'+str(self.number))
            return self.iterator if one_shot else iter((self.number,self.letter))
    def selected(number,letter):trace.append('filter:'+str(number));return number%2==1
    def emit(number,letter):trace.append('emit:'+str(number));return str(number)+letter
    environment=jinja2.Environment(extensions=['jinja2.ext.loopcontrols'] if break_with_else else [])
    control='{% break %}{% else %}E' if break_with_else else ''
    source='{% for number, letter in pairs if selected(number,letter) %}{{ emit(number,letter) }}'+control+'{% endfor %}'
    doc=Document();doc.add_paragraph(source);buffer=io.BytesIO();doc.save(buffer)
    template=engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
    template.render({'pairs':[Pair(1,'A'),Pair(2,'B'),Pair(3,'C')],
                     'selected':selected,'emit':emit},jinja_env=environment)
    output=io.BytesIO();template.save(output);blob=output.getvalue()
    return Result({'text':Document(io.BytesIO(blob)).paragraphs[0].text,'trace':trace},{'document.docx':blob})


def filtered_nested_destructure(engine):
    trace=[]
    class Inner:
        def __init__(self,number):self.number=number
        def __iter__(self):trace.append('inner:'+str(self.number));return iter(('A','B'))
    class Outer:
        def __init__(self,number):self.number=number
        def __iter__(self):trace.append('outer:'+str(self.number));return iter((self.number,Inner(self.number)))
    def selected(number):trace.append('filter:'+str(number));return number%2==1
    def emit(number):trace.append('emit:'+str(number));return number
    source='{% for number, (first,second) in pairs if selected(number) %}{{ emit(number) }}{% else %}E{% endfor %}'
    doc=Document();doc.add_paragraph(source);buffer=io.BytesIO();doc.save(buffer)
    template=engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
    template.render({'pairs':[Outer(1),Outer(2)],'selected':selected,'emit':emit})
    output=io.BytesIO();template.save(output);blob=output.getvalue()
    return Result({'text':Document(io.BytesIO(blob)).paragraphs[0].text,'trace':trace},{'document.docx':blob})


def filtered_unpack_error(engine, *, mode='exception', assignment=False):
    trace=[];error=RuntimeError('pair failed')
    class Pair:
        def __init__(self):self.index=0
        def __iter__(self):trace.append('iter');return self
        def __next__(self):
            index=self.index;self.index+=1;trace.append('next:'+str(index))
            if mode=='exception' and index==1:raise error
            if mode=='late_exception' and index==2:raise error
            if mode=='short' and index==1:raise StopIteration
            if index==5:raise StopIteration
            return index
    def selected(value):trace.append('filter');return True
    def emit(value):trace.append('emit');return value
    source=('{% set first, second = pair %}{{ emit(first) }}' if assignment else
            '{% for first, second in pairs if selected(first) %}{{ emit(first) }}{% endfor %}')
    doc=Document();doc.add_paragraph(source);buffer=io.BytesIO();doc.save(buffer)
    template=engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
    context={'selected':selected,'emit':emit}
    context['pair' if assignment else 'pairs']=Pair() if assignment else [Pair()]
    try:template.render(context)
    except Exception as actual:
        return Result({'type':type(actual).__module__+'.'+type(actual).__name__,'message':str(actual),
                       'same_instance':actual is error,'trace':trace},{})
    raise AssertionError('expected unpacking error')


def filtered_unpack_noniterable(engine, *, none=False, assignment=False):
    trace=[]
    def selected(value):trace.append('filter');return True
    source=('{% set first, second = pair %}{{ first }}' if assignment else
            '{% for first, second in pairs if selected(first) %}{{ first }}{% endfor %}')
    doc=Document();doc.add_paragraph(source);buffer=io.BytesIO();doc.save(buffer)
    template=engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
    value=None if none else 42
    context={'selected':selected,'pair' if assignment else 'pairs':value if assignment else [value]}
    try:template.render(context)
    except Exception as actual:
        return Result({'type':type(actual).__module__+'.'+type(actual).__name__,
                       'message':str(actual),'trace':trace},{})
    raise AssertionError('expected non-iterable unpacking error')


def filtered_unpack_iter_method_error(engine, *, disabled=False, sequence=False):
    trace=[];error=TypeError('iterator failed')
    class Broken:
        def __iter__(self):trace.append('iter');raise error
    class Disabled:
        __iter__=None
    class Sequence:
        def __getitem__(self,index):trace.append('getitem:'+str(index));raise error
    source='{% for first, second in pairs if first %}{{ first }}{% endfor %}'
    doc=Document();doc.add_paragraph(source);buffer=io.BytesIO();doc.save(buffer)
    template=engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
    value=Disabled() if disabled else Sequence() if sequence else Broken()
    try:template.render({'pairs':[value]})
    except Exception as actual:
        return Result({'type':type(actual).__module__+'.'+type(actual).__name__,
                       'message':str(actual),'same_instance':actual is error,'trace':trace},{})
    raise AssertionError('expected iterator setup error')


def filtered_unpack_extra_object(engine, *, early=False, short=False):
    trace=[];error=RuntimeError('extra inspected')
    class Extra:
        @property
        def __class__(self):trace.append('extra_class');raise error
    class Pair:
        def __init__(self):self.index=0
        def __iter__(self):trace.append('iter');return self
        def __next__(self):
            index=self.index;self.index+=1;trace.append('next:'+str(index))
            if (early or short) and index==0:return Extra()
            if short and index==1:raise StopIteration
            if index<2:return index
            if index==2:return Extra()
            raise StopIteration
    source='{% for first, second in pairs if first %}{{ first }}{% endfor %}'
    doc=Document();doc.add_paragraph(source);buffer=io.BytesIO();doc.save(buffer)
    template=engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
    try:template.render({'pairs':[Pair()]})
    except Exception as actual:
        return Result({'type':type(actual).__module__+'.'+type(actual).__name__,
                       'message':str(actual),'same_instance':actual is error,'trace':trace},{})
    raise AssertionError('expected unpacking error')


def filtered_unpack_next_lookup(engine):
    trace=[]
    class Pair:
        def __init__(self):self.index=0
        def __getattribute__(self,name):
            if name=='__next__':trace.append('next_lookup');raise RuntimeError('next lookup')
            return object.__getattribute__(self,name)
        def __iter__(self):trace.append('iter');return self
        def __next__(self):
            index=self.index;self.index+=1;trace.append('next:'+str(index))
            if index==2:raise StopIteration
            return index+1
    def selected(value):trace.append('filter');return True
    def emit(value):trace.append('emit');return value
    source='{% for first, second in pairs if selected(first) %}{{ emit(first) }}{% endfor %}'
    doc=Document();doc.add_paragraph(source);buffer=io.BytesIO();doc.save(buffer)
    template=engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
    template.render({'pairs':[Pair()],'selected':selected,'emit':emit})
    output=io.BytesIO();template.save(output);blob=output.getvalue()
    return Result({'text':Document(io.BytesIO(blob)).paragraphs[0].text,'trace':trace},{'document.docx':blob})


def filtered_unpack_unused_opaque(engine):
    trace=[]
    class Extra:
        @property
        def __class__(self):trace.append('extra_class');raise RuntimeError('unused value inspected')
    class Pair:
        def __iter__(self):trace.append('iter');return iter((Extra(),1))
    source='{% for first, second in pairs if second %}ok{% endfor %}'
    doc=Document();doc.add_paragraph(source);buffer=io.BytesIO();doc.save(buffer)
    template=engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
    template.render({'pairs':[Pair()]})
    output=io.BytesIO();template.save(output);blob=output.getvalue()
    return Result({'text':Document(io.BytesIO(blob)).paragraphs[0].text,'trace':trace},{'document.docx':blob})


def filtered_unpack_opaque_use(engine, *, mode):
    trace=[];error=RuntimeError('class inspected')
    class Extra:
        @property
        def __class__(self):trace.append('class');raise error
        def __str__(self):trace.append('str');return 'X'
    class Pair:
        def __iter__(self):trace.append('iter');return iter((Extra(),1))
    def selected(value):trace.append('filter');return True
    expression={'string':'first','none':'first is none','sameas':'first is sameas(first)',
                'defined':'first is defined','default':"first|default('fallback')"}[mode]
    source='{% for first, second in pairs if selected(second) %}{{ '+expression+' }}{% endfor %}'
    doc=Document();doc.add_paragraph(source);buffer=io.BytesIO();doc.save(buffer)
    template=engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
    try:template.render({'pairs':[Pair()],'selected':selected})
    except RuntimeError as actual:
        return Result({'type':type(actual).__module__+'.'+type(actual).__name__,'message':str(actual),
                       'same_instance':actual is error,'trace':trace},{})
    output=io.BytesIO();template.save(output);blob=output.getvalue()
    return Result({'text':Document(io.BytesIO(blob)).paragraphs[0].text,'trace':trace},{'document.docx':blob})


def filtered_unpack_undefined(engine, *, strict=False):
    trace=[]
    missing=(jinja2.StrictUndefined if strict else jinja2.Undefined)(name='missing')
    class Pair:
        def __iter__(self):trace.append('iter');return iter((missing,1))
    def selected(value):trace.append('filter');return True
    def identity(value):trace.append('callback');return value
    source="{% for first, second in pairs if selected(second) %}{{ first is defined }}/{{ first is undefined }}/{{ first|default('fallback') }}/{{ identity(first) is sameas(first) }}{% endfor %}"
    doc=Document();doc.add_paragraph(source);buffer=io.BytesIO();doc.save(buffer)
    template=engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
    template.render({'pairs':[Pair()],'selected':selected,'identity':identity})
    output=io.BytesIO();template.save(output);blob=output.getvalue()
    return Result({'text':Document(io.BytesIO(blob)).paragraphs[0].text,'trace':trace},{'document.docx':blob})



def filtered_metadata(source, values):
    """Record when metadata lookahead consumes and filters additional values."""
    def execute(engine):
        trace=[]
        def selected(value):
            trace.append('filter:'+str(value))
            return value%2==1
        def emit(value):
            trace.append('emit:'+str(value))
            return value
        environment=jinja2.Environment()
        environment.tests['selected']=selected
        doc=Document();doc.add_paragraph(source);buffer=io.BytesIO();doc.save(buffer)
        template=engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
        template.render({'values':iter(values),'emit':emit},jinja_env=environment)
        output=io.BytesIO();template.save(output);blob=output.getvalue()
        return Result({'text':Document(io.BytesIO(blob)).paragraphs[0].text,'trace':trace},{'document.docx':blob})
    return execute



def filtered_generator_error(engine, *, with_last=False, with_else=False):
    trace=[]
    failure=RuntimeError("iteration failed")
    def values():
        trace.append('next:1')
        yield 1
        trace.append('next:error')
        raise failure
    def selected(value):
        trace.append('filter:'+str(value))
        return True
    def emit(value):
        trace.append('emit:'+str(value))
        return value
    document=Document()
    metadata = ':{{ loop.last }}' if with_last else ''
    document.add_paragraph('{% for x in values if selected(x) %}{{ emit(x) }}' + metadata + ('{% else %}E' if with_else else '') + '{% endfor %}')
    source=io.BytesIO()
    document.save(source)
    source.seek(0)
    template=engine.DocxTemplate(source)
    try:
        template.render({'values':values(),'selected':selected,'emit':emit})
    except RuntimeError as error:
        return Result({'type':type(error).__module__+'.'+type(error).__name__,
                       'same_instance':error is failure,'message':str(error),'trace':trace}, {})
    raise AssertionError('expected the generator exception')


PROBES={
 'iteration_filter_context_scope':Probe(filtered_context_scope,{'text':'1:False;3:True;',
     'trace':[['filter',1,'outer',False],['emit',1],['filter',2,'outer',False],['filter',3,'outer',False],['emit',3]]}),
 'iteration_filter_last_generator_error':Probe(lambda engine: filtered_generator_error(engine, with_last=True),{'type':'builtins.RuntimeError','same_instance':True,'message':'iteration failed','trace':['next:1','filter:1','emit:1','next:error']},True),
 'iteration_filter_generator_error':Probe(filtered_generator_error,{'type':'builtins.RuntimeError','same_instance':True,'message':'iteration failed','trace':['next:1','filter:1','emit:1','next:error']},True),
 'iteration_filter_else_generator_error':Probe(lambda engine: filtered_generator_error(engine, with_else=True),{'type':'builtins.RuntimeError','same_instance':True,'message':'iteration failed','trace':['next:1','filter:1','emit:1','next:error']},True),
 'iteration_filter_length_after_body':Probe(filtered_metadata('{% for value in values if value is selected %}{{ emit(value) }}:{{ loop.length }};{% endfor %}',[1,2,3]),{'text':'1:2;3:2;','trace':['filter:1','emit:1','filter:2','filter:3','emit:3']}),
 'iteration_filter_nextitem_lookahead':Probe(filtered_metadata('{% for value in values if value is selected %}{{ emit(value) }}:{{ loop.nextitem|default("end") }};{% endfor %}',[1,2,3]),{'text':'1:3;3:end;','trace':['filter:1','emit:1','filter:2','filter:3','emit:3']}),
 'iteration_filter_last_lookahead':Probe(filtered_metadata('{% for value in values if value is selected %}{{ emit(value) }}:{{ loop.last }};{% endfor %}',[1,2,3]),{'text':'1:False;3:True;','trace':['filter:1','emit:1','filter:2','filter:3','emit:3']}),
 'iteration_filter_length_consumption':Probe(filtered_metadata('{% for value in values if value is selected %}{{ loop.length }}:{{ emit(value) }};{% endfor %}',[1,2,3]),{'text':'2:1;2:3;','trace':['filter:1','filter:2','filter:3','emit:1','emit:3']}),
 'iteration_filter_empty_else':Probe(filtered_metadata('{% for value in values if value is selected %}{{ emit(value) }}{% else %}empty{% endfor %}',[2,4]),{'text':'empty','trace':['filter:2','filter:4']}),
 'iteration_filter_lazy_order':Probe(filtered_order(),{'text':'13','trace':['filter:1','emit:1','filter:2','filter:3','emit:3']}),
 'iteration_filter_identifier_substring':Probe(filtered_order(suffix='{{ loopy }}'),{'text':'1L3L','trace':['filter:1','emit:1','filter:2','filter:3','emit:3']}),
 'iteration_filter_else_lazy':Probe(filtered_else_order([1,2,3]),{'text':'13','trace':['filter:1','emit:1','filter:2','filter:3','emit:3']}),
 'iteration_filter_else_empty_trace':Probe(filtered_else_order([2,4]),{'text':'E','trace':['filter:2','filter:4']}),
 'iteration_filter_else_break_trace':Probe(filtered_else_order([1,2,3],'{% break %}'),{'text':'1E','trace':['filter:1','emit:1']}),
 'iteration_filter_else_continue_trace':Probe(filtered_else_order([1,3],'{% continue %}'),{'text':'13E','trace':['filter:1','emit:1','filter:3','emit:3']}),
 'iteration_filter_else_completed_trace':Probe(filtered_else_order([1,2,3],'{% if value == 3 %}{% continue %}{% endif %}'),{'text':'13','trace':['filter:1','emit:1','filter:2','filter:3','emit:3']}),
 'iteration_filter_else_nested':Probe(filtered_else_nested,{'text':'1;E;','trace':['filter:1','emit:1','filter:2','filter:2','filter:4']}),
 'iteration_filter_else_nested_break':Probe(lambda engine: filtered_else_nested(engine,break_from_else=True),{'text':'E','trace':['filter:2','filter:4']}),
 'iteration_filter_destructure':Probe(filtered_destructure,{'text':'1A3C','trace':['unpack:1','filter:1','emit:1','unpack:2','filter:2','unpack:3','filter:3','emit:3']}),
 'iteration_filter_destructure_break_else':Probe(lambda engine: filtered_destructure(engine,break_with_else=True),{'text':'1AE','trace':['unpack:1','filter:1','emit:1']}),
 'iteration_filter_destructure_one_shot':Probe(lambda engine: filtered_destructure(engine,one_shot=True),{'text':'1A3C','trace':['unpack:1','filter:1','emit:1','unpack:2','filter:2','unpack:3','filter:3','emit:3']}),
 'iteration_filter_nested_destructure':Probe(filtered_nested_destructure,{'text':'1','trace':['outer:1','inner:1','filter:1','emit:1','outer:2','inner:2','filter:2']}),
 'iteration_filter_destructure_excess':Probe(lambda engine: filtered_unpack_error(engine,mode='excess'),{'type':'builtins.ValueError','message':'too many values to unpack (expected 2)','same_instance':False,'trace':['iter','next:0','next:1','next:2']},True),
 'iteration_filter_destructure_exception':Probe(filtered_unpack_error,{'type':'builtins.RuntimeError','message':'pair failed','same_instance':True,'trace':['iter','next:0','next:1']},True),
 'iteration_filter_destructure_short':Probe(lambda engine: filtered_unpack_error(engine,mode='short'),{'type':'builtins.ValueError','message':'not enough values to unpack (expected 2, got 1)','same_instance':False,'trace':['iter','next:0','next:1']},True),
 'iteration_filter_destructure_late_exception':Probe(lambda engine: filtered_unpack_error(engine,mode='late_exception'),{'type':'builtins.RuntimeError','message':'pair failed','same_instance':True,'trace':['iter','next:0','next:1','next:2']},True),
 'iteration_filter_destructure_noniterable':Probe(filtered_unpack_noniterable,{'type':'builtins.TypeError','message':'cannot unpack non-iterable int object','trace':[]},True),
 'iteration_filter_destructure_none':Probe(lambda engine: filtered_unpack_noniterable(engine,none=True),{'type':'builtins.TypeError','message':'cannot unpack non-iterable NoneType object','trace':[]},True),
 'iteration_filter_destructure_iter_error':Probe(filtered_unpack_iter_method_error,{'type':'builtins.TypeError','message':'iterator failed','same_instance':True,'trace':['iter']},True),
 'iteration_filter_destructure_iter_disabled':Probe(lambda engine: filtered_unpack_iter_method_error(engine,disabled=True),{'type':'builtins.TypeError','message':"'Disabled' object is not iterable",'same_instance':False,'trace':[]},True),
 'iteration_filter_destructure_sequence_error':Probe(lambda engine: filtered_unpack_iter_method_error(engine,sequence=True),{'type':'builtins.TypeError','message':'iterator failed','same_instance':True,'trace':['getitem:0']},True),
 'assignment_unpack_excess':Probe(lambda engine: filtered_unpack_error(engine,mode='excess',assignment=True),{'type':'builtins.ValueError','message':'too many values to unpack (expected 2)','same_instance':False,'trace':['iter','next:0','next:1','next:2']},True),
 'assignment_unpack_exception':Probe(lambda engine: filtered_unpack_error(engine,assignment=True),{'type':'builtins.RuntimeError','message':'pair failed','same_instance':True,'trace':['iter','next:0','next:1']},True),
 'assignment_unpack_none':Probe(lambda engine: filtered_unpack_noniterable(engine,none=True,assignment=True),{'type':'builtins.TypeError','message':'cannot unpack non-iterable NoneType object','trace':[]},True),
 'iteration_filter_destructure_extra_object':Probe(filtered_unpack_extra_object,{'type':'builtins.ValueError','message':'too many values to unpack (expected 2)','same_instance':False,'trace':['iter','next:0','next:1','next:2']},True),
 'iteration_filter_destructure_early_object':Probe(lambda engine: filtered_unpack_extra_object(engine,early=True),{'type':'builtins.ValueError','message':'too many values to unpack (expected 2)','same_instance':False,'trace':['iter','next:0','next:1','next:2']},True),
 'iteration_filter_destructure_short_object':Probe(lambda engine: filtered_unpack_extra_object(engine,short=True),{'type':'builtins.ValueError','message':'not enough values to unpack (expected 2, got 1)','same_instance':False,'trace':['iter','next:0','next:1']},True),
 'iteration_filter_destructure_next_lookup':Probe(filtered_unpack_next_lookup,{'text':'1','trace':['iter','next:0','next:1','next:2','filter','emit']}),
 'iteration_filter_destructure_unused_opaque':Probe(filtered_unpack_unused_opaque,{'text':'ok','trace':['iter']}),
 'iteration_filter_destructure_opaque_string':Probe(lambda engine: filtered_unpack_opaque_use(engine,mode='string'),{'text':'X','trace':['iter','filter','str']}),
 'iteration_filter_destructure_opaque_none':Probe(lambda engine: filtered_unpack_opaque_use(engine,mode='none'),{'text':'False','trace':['iter','filter']}),
 'iteration_filter_destructure_opaque_sameas':Probe(lambda engine: filtered_unpack_opaque_use(engine,mode='sameas'),{'text':'True','trace':['iter','filter']}),
 'iteration_filter_destructure_opaque_defined':Probe(lambda engine: filtered_unpack_opaque_use(engine,mode='defined'),{'type':'builtins.RuntimeError','message':'class inspected','same_instance':True,'trace':['iter','filter','class']},True),
 'iteration_filter_destructure_opaque_default':Probe(lambda engine: filtered_unpack_opaque_use(engine,mode='default'),{'type':'builtins.RuntimeError','message':'class inspected','same_instance':True,'trace':['iter','filter','class']},True),
 'iteration_filter_destructure_undefined':Probe(filtered_unpack_undefined,{'text':'False/True/fallback/True','trace':['iter','filter','callback']}),
 'iteration_filter_destructure_strict_undefined':Probe(lambda engine: filtered_unpack_undefined(engine,strict=True),{'text':'False/True/fallback/True','trace':['iter','filter','callback']}),
 'iteration_filter_break_consumption':Probe(filtered_order(True),{'text':'1','trace':['filter:1','emit:1']}),
}
