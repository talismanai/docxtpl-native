"""Actual object ownership and phase commits on an exposed Python document."""
import io
from docx import Document
from docx.opc.part import XmlPart
from .protocol_base import Probe, Result, template, observed


def save(document):
    buffer=io.BytesIO();document.save(buffer);return buffer.getvalue()


def body_commit_before_header(engine):
    document=engine.DocxTemplate(io.BytesIO(template('{{ change() }}{{ value }}','{{ inspect() }}')))
    live=document.get_docx();old=live.paragraphs[0];trace=[]
    def change():
        old.runs[0].italic=True
        live.core_properties.subject='changed while rendering'
        trace.append(['body',live.paragraphs[0]._element is old._element])
        return 'B'
    def inspect():
        trace.append(['header',live.paragraphs[0].text,live.paragraphs[0]._element is old._element,
                      live._element.body.xpath('string(.)')])
        return 'H'
    document.render({'change':change,'inspect':inspect,'value':'X'})
    blob=save(document);current=Document(io.BytesIO(blob))
    return observed(blob,trace,retained_text=old.text,retained_italic=old.runs[0].italic,
                    output_italic=current.paragraphs[0].runs[0].italic,
                    subject=current.core_properties.subject,header=current.sections[0].header.paragraphs[0].text,
                    same_document=document.docx is live)


def header_part_replacement(engine):
    document=engine.DocxTemplate(io.BytesIO(template('B','{{ value }}')))
    live=document.get_docx();old=live.sections[0].header.part
    uri=document.HEADER_URI
    identity=next(key for key,rel in live._part.rels.items() if rel.reltype==uri)
    document.render({'value':'H'})
    new=live._part.rels[identity].target_part
    return observed(save(document),old_text=old.element[0].text,
                    replaced=new is not old,exact_xml_part=type(new) is XmlPart,
                    same_package=new.package is old.package)


def property_partial_commit(engine):
    source=Document();source.add_paragraph('{{ value }}')
    source.core_properties.author='{{ author }}';source.core_properties.comments='{{ bad }}'
    source.core_properties.title='unreached'
    buffer=io.BytesIO();source.save(buffer)
    document=engine.DocxTemplate(io.BytesIO(buffer.getvalue()));live=document.get_docx()
    properties=live.core_properties
    try:document.render({'value':'B','author':'updated','bad':'X'*256})
    except ValueError as error:
        observation={'error':str(error),'body':live._element.body.xpath('string(.)'),'author':properties.author,
                     'comments':properties.comments,'title':properties.title,
                     'flags':[document.is_rendered,document.is_saved],'same_document':document.docx is live}
    else:raise AssertionError('property setter failure was suppressed')
    # save after a failed first render reloads the original template. Retained
    # references must still show the partial commits on the old model.
    output=save(document)
    observation.update(saved_body=Document(io.BytesIO(output)).paragraphs[0].text,
                       retained_author=properties.author,reloaded=document.docx is not live)
    return Result(observation,{'document.docx':output})


def subdocument_builder(engine):
    document=engine.DocxTemplate(io.BytesIO(template('{{p subdoc }}')))
    subdoc=document.new_subdoc();paragraph=subdoc.add_paragraph('A');paragraph.add_run('B').bold=True
    table=subdoc.add_table(rows=1,cols=2);table.cell(0,0).text='C';table.cell(0,1).text='D'
    document.render({'subdoc':subdoc});blob=save(document);parsed=Document(io.BytesIO(blob))
    return observed(blob,table=[cell.text for cell in parsed.tables[0].rows[0].cells],
                    bold=parsed.paragraphs[0].runs[1].bold,
                    shared_part=subdoc.part is document.docx.part)


def live_image_relationship(engine):
    from PIL import Image
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    image=io.BytesIO();Image.new('RGB',(4,2),(21,42,84)).save(image,format='PNG');image.seek(0)
    document=engine.DocxTemplate(io.BytesIO(template('{{ image() }}{{ inspect() }}')))
    live=document.get_docx();trace=[]
    def make_image():
        trace.append(['before',sum(rel.reltype==RT.IMAGE for rel in live.part.rels.values())])
        return engine.InlineImage(document,image,width=5,title='callback')
    def inspect():
        trace.append(['after',sum(rel.reltype==RT.IMAGE for rel in live.part.rels.values())])
        return 'X'
    document.render({'image':make_image,'inspect':inspect})
    return observed(save(document),trace,current_part=document.current_rendering_part is live.part)


def live_hyperlink_relationship(engine):
    document=engine.DocxTemplate(io.BytesIO(template('{{r make() }}')))
    live=document.get_docx();trace=[]
    def make():
        identity=document.build_url_id('https://example.test/live')
        trace.append([identity,live.part.rels[identity].target_ref])
        return engine.RichText('link',url_id=identity)
    document.render({'make':make})
    return observed(save(document),trace)


def callback_document_access(engine):
    from docx.document import Document as DocumentType
    from lxml import etree
    document=engine.DocxTemplate(io.BytesIO(template('{{ inspect() }}','{{ header() }}')))
    retained=[];trace=[]
    def inspect():
        live=document.get_docx();retained.append(live)
        trace.append([isinstance(live,DocumentType),isinstance(live._element,etree._Element),
                      document.current_rendering_part is live.part])
        return 'B'
    def header():
        trace.append([document.docx is retained[0],document.docx._element.body.xpath('string(.)')])
        return 'H'
    document.render({'inspect':inspect,'header':header})
    return observed(save(document),trace,same_document=document.docx is retained[0])


def unescaped_body_recovery(engine):
    from .protocol_base import generate
    return observed(generate(engine,'{{ value }}',{'value':'Rock & Roll'}))


def live_footnote_render(engine):
    import zipfile
    from lxml import etree
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    document=engine.DocxTemplate(io.BytesIO(template('{{ value }}')))
    live=document.get_docx();part=live.part;trace=[]
    namespace='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    xml=f'<w:footnotes xmlns:w="{namespace}"><w:footnote w:id="1"><w:p><w:r><w:t>{{{{ note() }}}}</w:t></w:r></w:p></w:footnote></w:footnotes>'
    note=Part(PackURI('/word/footnotes.xml'),
        'application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml',xml.encode(),part.package)
    part.relate_to(note,'http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes')
    def note_callback():trace.append(['note',document.current_rendering_part is note]);return 'Note'
    document.render({'value':'Body','note':note_callback});blob=save(document)
    with zipfile.ZipFile(io.BytesIO(blob)) as package:
        root=etree.fromstring(package.read('word/footnotes.xml'))
        texts=root.xpath('//w:t/text()',namespaces={'w':namespace})
    return observed(blob,trace,footnote=texts,same_document=document.docx is live)


PROBES={
    'body_commit_before_header':Probe(body_commit_before_header,{'text':'BX',
        'trace':[['body',True],['header','{{ change() }}{{ value }}',True,'BX']],
        'retained_text':'{{ change() }}{{ value }}',
        'retained_italic':True,'output_italic':None,'subject':'changed while rendering','header':'H','same_document':True}),
    'header_part_replacement':Probe(header_part_replacement,{'text':'B','old_text':'{{ value }}',
        'replaced':True,'exact_xml_part':True,'same_package':True}),
    'property_partial_commit':Probe(property_partial_commit,{'error':"exceeded 255 char limit for property, got:\n\n'"+'X'*256+"'",
        'body':'B','author':'updated','comments':'{{ bad }}','title':'unreached','flags':[False,False],
        'same_document':True,'saved_body':'{{ value }}','retained_author':'updated','reloaded':True}),
    'subdocument_builder':Probe(subdocument_builder,{'text':'AB','table':['C','D'],'bold':True,'shared_part':True}),
    'live_image_relationship':Probe(live_image_relationship,{'text':'X','trace':[['before',0],['after',1]],'current_part':True}),
    'live_hyperlink_relationship':Probe(live_hyperlink_relationship,{'text':'link','trace':[['rId9','https://example.test/live']]}),
    'callback_document_access':Probe(callback_document_access,{'text':'B','trace':[[True,True,True],[True,'B']],'same_document':True}),
    'unescaped_body_recovery':Probe(unescaped_body_recovery,{'text':'Rock  Roll'}),
    'live_footnote_render':Probe(live_footnote_render,{'text':'Body','trace':[['note',True]],'footnote':['Note'],'same_document':True}),
}
