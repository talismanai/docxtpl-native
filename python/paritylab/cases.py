"""Deterministic synthetic fixtures. No production documents are required."""
import base64
import io
from pathlib import Path
from docx import Document
from docx.oxml import OxmlElement
from docx.shared import Mm, RGBColor
import jinja2

CASES = {
 'plain':'plain','unicode':'plain','if':'conditional','loop':'loop','table_rows':'table',
 'formatting':'formatting','header_footer':'header_footer','listing':'plain','richtext':'plain',
 'image':'plain','subdocument':'plain','finalize':'plain','pass_context':'plain','loader':'plain',
 'strict_undefined':'plain','large_integer':'plain','macro':'plain','split_tag':'split',
 'long_paragraphs':'long','long_table':'table','bool_none':'plain','dict_order':'plain',
 'xml_comment':'comment','replace_media':'media','foreign_lookup':'plain',
}

class LookupRecord:
    """Force attribute/item calls through the foreign object runtime."""
    __slots__ = ('label', 'value')

    def __init__(self, index):
        self.label = 'row'
        self.value = str(index)

    def __getitem__(self, key):
        if key == 'value':
            return self.value
        raise KeyError(key)

def fixture(name, directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    d=Document();kind=CASES[name]
    texts={
      'plain':'Café {{ customer }}!', 'unicode':'{{ value }}',
      'if':'{% if enabled %}Yes{% else %}No{% endif %}',
      'loop':'{% for x in items %}{{ x }}{% if not loop.last %}, {% endif %}{% endfor %}',
      'listing':'{{ listing }}','richtext':'{{r rich }}','image':'{{ image }}','subdocument':'{{p subdoc }}',
      'finalize':'{{ value }}','pass_context':'{{ value|prefix }}',
      'loader':"{% include 'fragment' %}",'strict_undefined':'{{ missing }}',
      'large_integer':'{{ value + 1 }}','macro':'{% macro m() %}{{ varargs|join(",") }}{% endmacro %}{{ m(1,2) }}',
      'bool_none':'{{ yes }}|{{ no }}|{{ nothing }}',
      'dict_order':'{% for k,v in mapping.items() %}{{ k }}={{ v }};{% endfor %}',
      'foreign_lookup':'{% for item in rows %}{{ item.label }}={{ item["value"] }};{% endfor %}',
    }
    if kind=='table':
        table=d.add_table(rows=4,cols=2)
        table.cell(0,0).text='Name';table.cell(0,1).text='Price'
        table.cell(1,0).text='{%tr for item in items %}'
        table.cell(2,0).text='{{ item.name }}';table.cell(2,1).text='{{ item.price }}'
        table.cell(3,0).text='{%tr endfor %}'
    elif kind=='split':
        p=d.add_paragraph();p.add_run('{{ cus');p.add_run('tomer }}').bold=True
    elif kind=='long':
        for i in range(1000):d.add_paragraph('Cláusula %d — {{ customer }}.'%i)
    else:
        p=d.add_paragraph(texts.get(name,'{{ customer }}'))
        if kind=='formatting':
            p.style='Heading 1';r=p.add_run(' Fixed text');r.bold=True;r.italic=True
            r.font.name='Liberation Serif';r.font.color.rgb=RGBColor(12,34,56)
            p.paragraph_format.left_indent=Mm(5)
        if kind=='header_footer':
            d.sections[0].header.paragraphs[0].text='Header {{ customer }}'
            d.sections[0].footer.paragraphs[0].text='Rodapé {{ customer }}'
        if kind=='comment':
            from lxml import etree
            p._p.append(etree.Comment('preserve-this-comment'))
    png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII=')
    # Generate image with Pillow when possible, avoiding decoder variance.
    from PIL import Image
    img=io.BytesIO();Image.new('RGB',(24,16),(220,180,40)).save(img,format='PNG');png=img.getvalue()
    (directory/'image.png').write_bytes(png)
    img=io.BytesIO();Image.new('RGB',(24,16),(40,100,180)).save(img,format='PNG')
    (directory/'replacement.png').write_bytes(img.getvalue())
    if name=='replace_media':d.add_picture(io.BytesIO(png),width=Mm(12))
    sub=Document();sub.add_paragraph('Subdocumento com estilo').runs[0].italic=True
    sub.save(directory/'subdoc.docx')
    path=directory/'template.docx';d.save(path);return path

def inputs(name,lib,tpl,directory):
    directory=Path(directory)
    context={'customer':'Maurício e família'};env=None;autoescape=False
    if name=='unicode':context['value']='façade — 中文 — 👶 — café';autoescape=True
    if name=='if':context['enabled']=True
    if name=='loop':context['items']=['A','B','C']
    if name in ('table_rows','long_table'):
        context['items']=[{'name':'item-%d'%i,'price':i*3} for i in range(2000 if name=='long_table' else 4)]
    if name=='listing':context['listing']=lib.Listing('Line 1\nLine 2\tcolumn\aparagraph')
    if name=='richtext':context['rich']=lib.RichText('Rich text',bold=True,color='123456')
    if name=='image':context['image']=lib.InlineImage(tpl,str(directory/'image.png'),width=Mm(20))
    if name=='subdocument':context['subdoc']=tpl.new_subdoc(str(directory/'subdoc.docx'))
    if name=='finalize':context['value']=None;env=jinja2.Environment(finalize=lambda v:'EMPTY' if v is None else v)
    if name=='pass_context':
        env=jinja2.Environment()
        @jinja2.pass_context
        def prefix(ctx,v):return ctx['prefix']+v
        env.filters['prefix']=prefix;context.update(value='X',prefix='P')
    if name=='loader':env=jinja2.Environment(loader=jinja2.DictLoader({'fragment':'Incluído {{ customer }}'}))
    if name=='strict_undefined':env=jinja2.Environment(undefined=jinja2.StrictUndefined)
    if name=='large_integer':context['value']=10**100
    if name=='bool_none':context.update(yes=True,no=False,nothing=None)
    if name=='dict_order':context['mapping']={'z':1,'a':2}
    if name=='foreign_lookup':context['rows']=[LookupRecord(index) for index in range(1000)]
    if name=='replace_media':tpl.replace_media(str(directory/'image.png'),str(directory/'replacement.png'))
    return context,env,autoescape

