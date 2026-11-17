"""Inline picture names, detached XML whitespace and dimension rounding."""
import io
import zipfile
from typing import Any

from lxml import etree
from PIL import Image

from .opc_probes import rewrite
from .protocol_base import Probe, Result, generate, observed, template

WP='http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'

def image_contract(engine):
    blob=io.BytesIO();Image.new('RGB',(4,2),(21,42,84)).save(blob,format='PNG');blob.seek(0)
    document=engine.DocxTemplate(io.BytesIO(template('{{ image }}{{ image }}',header='{{ image }}')))
    image=engine.InlineImage(document,blob,width=5,anchor='https://example.test/image',title=42,descr=False)
    document.render({'image':image});output=io.BytesIO();document.save(output)
    pictures=[];extents=[]
    with zipfile.ZipFile(output) as package:
        for part in ['word/document.xml','word/header1.xml']:
            root=etree.fromstring(package.read(part))
            pictures.extend([[item.get('id'),item.get('name'),item.get('title'),item.get('descr')]
                             for item in root.iter('{'+WP+'}docPr')])
            extents.extend([[item.get('cx'),item.get('cy')] for item in root.iter('{'+WP+'}extent')])
    return Result({'pictures':pictures,'extents':extents},{'document.docx':output.getvalue()})

def header_xml_loading(engine):
    def mutate(parts):
        root=etree.fromstring(parts['word/header1.xml'])
        etree.indent(root,space='   ')
        parts['word/header1.xml']=etree.tostring(root,encoding='UTF-16',xml_declaration=True,standalone=True)
    source=rewrite(template('body',header='{{ label }}'),mutate)
    output=generate(engine,'',{'label':' X '},source=source)
    from docx import Document
    document=Document(io.BytesIO(output))
    return Result({'text':document.paragraphs[0].text,'header':document.sections[0].header.paragraphs[0].text},
                  {'document.docx':output})


def inline_image_constructor_protocol(engine):
    trace=[];source=object();template=object();width=object();error=ValueError('title failed')
    class Text:
        def __init__(self,name,fail=False):self.name=name;self.fail=fail
        def __str__(self):
            trace.append(self.name)
            if self.fail:raise error
            return self.name
    image=engine.InlineImage(template,source,width=width,title=Text('title'),descr=Text('description'))
    success={'source_identity':image.image_descriptor is source,'template_identity':image.tpl is template,
             'width_identity':image.width is width,'title':image.title,'descr':image.descr}
    broken=object.__new__(engine.InlineImage)
    try:broken.__init__(template,source,width=width,title=Text('broken',True),descr=Text('unreached'))
    except ValueError as actual:
        return Result({'success':success,'same_exception':actual is error,
                       'partial_fields':sorted(vars(broken)), 'trace':trace},{})
    raise AssertionError('constructor exception was not propagated')


def inline_image_representation_hooks(engine):
    trace=[]
    class Image(engine.InlineImage):
        def _insert_image(self):trace.append('insert');return 'image-xml'
    value=Image(None,'source')
    return Result({'str':str(value),'html':value.__html__(),
                   'unicode':value.__unicode__(),'trace':trace},{})

def inline_image_callback_with_file_subdoc(engine: Any) -> Result:
    # The unused file subdocument is a routing fixture in both roles. It is
    # never rendered by the reference and does not provide its expected output.
    import docxtpl_native

    image = engine.InlineImage(None, io.BytesIO(b'image'))
    child = object.__new__(docxtpl_native.Subdoc)
    child.tpl = None
    child.source = io.BytesIO(template('child'))
    trace: list[bool] = []

    def callback(value: object) -> str:
        trace.append(value is image)
        return 'same' if value is image else 'different'

    output = generate(engine, '{{ callback(image) }}',
                      {'child': child, 'image': image, 'callback': callback})
    return observed(output, trace)

def deferred_marker_user_text(engine: Any) -> Result:
    # The unused child selects file composition in the native candidate.
    # Both roles receive the same child fixture and a valid unused PNG image.
    import docxtpl_native
    from docx import Document

    image_data = io.BytesIO()
    Image.new('RGB', (2, 2), (21, 42, 84)).save(image_data, format='PNG')
    image_data.seek(0)
    image = engine.InlineImage(None, image_data)
    child = object.__new__(docxtpl_native.Subdoc)
    child.tpl = None
    child.source = io.BytesIO(template('child'))
    output = generate(engine, '{{ value }}',
                      {'child': child, 'image': image, 'value': '\x01DTPLD0\x01'})
    document = Document(io.BytesIO(output))
    return Result({'text': document.paragraphs[0].text, 'images': len(document.inline_shapes)},
                  {'document.docx': output})

PROBES={
    'image_contract':Probe(image_contract,{'pictures':[['1001','Picture 1','42','False'],['1002','Picture 1','42','False'],['1','Picture 1','42','False']],
                                         'extents':[['5','2']]*3}),
    'header_xml_loading':Probe(header_xml_loading,{'text':'body','header':' X '}),
    'inline_image_constructor_protocol':Probe(inline_image_constructor_protocol,{
        'success':{'source_identity':True,'template_identity':True,'width_identity':True,
                   'title':'title','descr':'description'},'same_exception':True,
        'partial_fields':['anchor','height','image_descriptor','tpl','width'],
        'trace':['title','description','broken']},True),
    'inline_image_representation_hooks':Probe(inline_image_representation_hooks,{
        'str':'image-xml','html':'image-xml','unicode':'image-xml',
        'trace':['insert','insert','insert']},True),
    'inline_image_callback_with_file_subdoc':Probe(inline_image_callback_with_file_subdoc,
                                                   {'text':'same','trace':[True]}),
    'deferred_marker_user_text':Probe(deferred_marker_user_text, {'text':'DTPLD0','images':0}),
}
