"""Rich XML builders retain identity and are formatted at the insertion point."""
import io
import jinja2
from docx import Document
from .protocol_base import Probe, Result, generate, observed


def rich_value_identity(engine):
    values=[engine.RichText(),engine.RichTextParagraph(),engine.Listing('')];trace=[]
    def check(value,index):
        trace.append([value is values[index],type(value).__name__,bool(value)])
        return str(index)
    return observed(generate(engine,'{{ check(rich,0) }}{{ check(paragraph,1) }}{{ check(listing,2) }}',
                             dict(zip(('rich','paragraph','listing'),values),check=check)),trace)


def rich_value_mutation(engine):
    rich=engine.RichText('A');listing=engine.Listing('before');trace=[]
    def change():
        rich.add('B',bold=True);listing.xml='after';trace.append('change');return ''
    blob=generate(engine,'{{r rich }}{{ change() }}{{r rich }}|{{ listing }}',
                  {'rich':rich,'listing':listing,'change':change},jinja2.Environment(autoescape=True))
    document=Document(io.BytesIO(blob))
    return observed(blob,trace,bold_text=[run.text for run in document.paragraphs[0].runs if run.bold])


def rich_value_callback(engine):
    trace=[]
    def rich():trace.append('rich');return engine.RichText('A<&>',bold=True)
    def paragraph():trace.append('paragraph');return engine.RichTextParagraph('P<&>')
    def listing():trace.append('listing');return engine.Listing('L<&>')
    blob=generate(engine,'{{r rich() }}|{{ listing() }}',{'rich':rich,'listing':listing},
                  jinja2.Environment(autoescape=True))
    # Paragraph insertion uses a complete containing paragraph, as required by
    # docxtpl's structural tag contract. Keep both documents for full comparison.
    second=generate(engine,'{{p paragraph() }}',{'paragraph':paragraph},jinja2.Environment(autoescape=True))
    result=observed(blob,trace,paragraphs=[item.text for item in Document(io.BytesIO(second)).paragraphs])
    result.outputs['paragraph.docx']=second
    return result



def rich_value_format_matrix(engine):
    rich=engine.RichText('A<&',bold=True,color='#AABBCC')
    rich.add('B',italic=True,underline='wave',font='eastAsia:Arial')
    rich.add('C',size=28,superscript=True)
    blob=generate(engine,'{{r value }}',{'value':rich},jinja2.Environment(autoescape=True))
    document=Document(io.BytesIO(blob))
    runs=[run for run in document.paragraphs[0].runs if run.text]
    return observed(blob, runs=[[run.text,run.bold is True,run.italic is True,
                                str(run.font.color.rgb) if run.font.color.rgb else None]
                               for run in runs])


def rich_value_object_properties(engine):
    trace=[]
    class Text(str):
        def replace(self,old,new):
            trace.append(['replace',old,new])
            return Text(super().replace(old,new))
    class Size:
        def __bool__(self):trace.append(['size','bool']);return True
        def __str__(self):trace.append(['size','str']);return '24'
    rich=engine.RichText()
    rich.add(Text('A<&>'),size=Size(),bold=True)
    return observed(generate(engine,'{{r value }}',{'value':rich}),trace)

def listing_escaped_text(engine):
    value = "A<&> ' \" é"
    blob = generate(engine, '{{ value }}', {'value':engine.Listing(value)},
                    jinja2.Environment(autoescape=True))
    return observed(blob)


def listing_public_values(engine):
    value=engine.Listing('A<&> "\' é')
    initial=value.xml
    observation={'xml':initial,'str':str(value),'html':value.__html__(),
                 'unicode':value.__unicode__(),'html_identity':value.__html__() is initial}
    replacement=''.join(['changed','-','value'])
    value.xml=replacement
    observation.update(mutated_html=value.__html__(),mutated_identity=value.__html__() is replacement)
    return Result(observation,{})


def rich_public_representation(engine):
    value=engine.RichText('A<&>')
    initial=value.xml
    observation={'str':str(value),'html':value.__html__(),
                 'html_identity':value.__html__() is initial}
    replacement=''.join(['changed','-','value'])
    value.xml=replacement
    observation.update(mutated_str=str(value),mutated_html=value.__html__(),
                       mutated_identity=value.__html__() is replacement)
    return Result(observation,{})


PROBES={
    'listing_escaped_text':Probe(listing_escaped_text,{'text':"A<&> ' \" é"}),
    'listing_public_values':Probe(listing_public_values,{'xml':'A&lt;&amp;&gt; &quot;&#x27; é',
        'str':'A&lt;&amp;&gt; &quot;&#x27; é','html':'A&lt;&amp;&gt; &quot;&#x27; é',
        'unicode':'A&lt;&amp;&gt; &quot;&#x27; é','html_identity':True,
        'mutated_html':'changed-value','mutated_identity':True},True),
    'rich_public_representation':Probe(rich_public_representation,{
        'str':'<w:r><w:t xml:space="preserve">A&lt;&amp;&gt;</w:t></w:r>',
        'html':'<w:r><w:t xml:space="preserve">A&lt;&amp;&gt;</w:t></w:r>',
        'html_identity':True,'mutated_str':'changed-value',
        'mutated_html':'changed-value','mutated_identity':True},True),
    'rich_value_format_matrix':Probe(rich_value_format_matrix,{'text':'A<&BC',
        'runs':[['A<&',True,False,'AABBCC'],['B',False,True,None],['C',False,False,None]]}),
    'rich_value_object_properties':Probe(rich_value_object_properties,{'text':'A<&>','trace':[
        ['replace','&','&amp;'],['replace','<','&lt;'],['replace','>','&gt;'],
        ['replace','"','&quot;'],['replace',"'",'&#x27;'],
        ['size','bool'],['size','str'],['size','str']]}),
    'rich_value_identity':Probe(rich_value_identity,{'text':'012','trace':[
        [True,'RichText',True],[True,'RichTextParagraph',True],[True,'Listing',True]]}),
    'rich_value_mutation':Probe(rich_value_mutation,{'text':'AAB|after','trace':['change'],'bold_text':['B']}),
    'rich_value_callback':Probe(rich_value_callback,{'text':'A<&>|L<&>','trace':['rich','listing','paragraph'],'paragraphs':['P<&>']}),
}
