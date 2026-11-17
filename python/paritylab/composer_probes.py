"""Composer observations on exposed document graphs."""
from .protocol_base import Probe, Result

def composer_diagrams(engine,missing=None):
    import importlib
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from lxml import etree
    relation='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    diagram='http://schemas.openxmlformats.org/drawingml/2006/diagram'
    types=(RT.DIAGRAM_DATA,RT.DIAGRAM_LAYOUT,RT.DIAGRAM_QUICK_STYLE,RT.DIAGRAM_COLORS)
    keys=('dm','lo','qs','cs');master=Document();child=Document();targets=[];ids=[]
    for index,kind in enumerate(types):
        part=Part(PackURI(f'/word/diagrams/test{index}.xml'),'application/xml',f'<n>{index}</n>'.encode(),child.part.package)
        targets.append(part);ids.append(child.part.relate_to(part,kind))
    attrs=' '.join(f'r:{key}="{rid}"' for key,rid in zip(keys,ids) if key!=missing)
    element=etree.fromstring(f'<root xmlns:dgm="{diagram}" xmlns:r="{relation}"><dgm:relIds {attrs}/></root>')
    node=element[0];composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
    try: result=composer.add_diagrams(child,element);error=None
    except Exception as exc: result=None;error={'type':type(exc).__name__,'args':list(exc.args)}
    transferred=[]
    for index,key in enumerate(keys):
        rid=node.get(f'{{{relation}}}{key}');rel=master.part.rels.get(rid)
        transferred.append(bool(rel is not None and rel.reltype==types[index] and rel.target_part is targets[index]))
    return Result({'result':result,'error':error,'transferred':transferred}, {})

def composer_diagrams_complete(engine): return composer_diagrams(engine)
def composer_diagrams_missing(engine): return composer_diagrams(engine,'qs')

def composer_section_types(engine):
    import importlib
    from docx import Document
    from docx.enum.section import WD_SECTION
    master=Document();master.add_section(WD_SECTION.CONTINUOUS);master.add_section(WD_SECTION.NEW_PAGE)
    child=Document();child.add_section(WD_SECTION.ODD_PAGE)
    result=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master).fix_section_types(child)
    return Result({'result':result,'master':[int(s.start_type) for s in master.sections],
                   'child':[int(s.start_type) for s in child.sections]}, {})

def composer_header_refs(engine):
    import importlib
    from docx import Document
    from lxml import etree
    w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    element=etree.fromstring(f'<w:sectPr xmlns:w="{w}"><w:headerReference/><w:footerReference/><w:pgSz/></w:sectPr>')
    result=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(Document()).remove_header_and_footer_references(None,element)
    return Result({'result':result,'remaining':[etree.QName(node).localname for node in element]}, {})

def composer_header_refs_partial(engine):
    import importlib
    import docxcompose.composer as composer
    from docx import Document
    trace=[];marker=RuntimeError('remove failed')
    class Parent:
        def remove(self,node):
            trace.append('remove:'+node.name)
            if node.name=='second': raise marker
    parent=Parent()
    class Ref:
        def __init__(self,name):self.name=name
        def getparent(self):trace.append('parent:'+self.name);return parent
    old=composer.xpath
    try:
        composer.xpath=lambda element,query: iter([Ref('first'),Ref('second')])
        obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(Document())
        try:obj.remove_header_and_footer_references(None,object());same=False
        except RuntimeError as exc:same=exc is marker
        return Result({'same_exception':same,'trace':trace}, {})
    finally:composer.xpath=old

def composer_header_refs_removed_module(engine):
    import importlib,sys
    from docx import Document
    from lxml import etree
    # Each engine's own subdocument module names the Composer whose globals it
    # honours (the package does not load docxtpl; see the api.module_globals
    # divergence).
    own=importlib.import_module(engine.__name__+'.subdoc')
    scope=own.Composer.remove_header_and_footer_references.__globals__
    previous_module=sys.modules.pop('docxcompose.composer',None)
    previous_xpath=scope['xpath'];trace=[]
    try:
        def callback(element,query):trace.append(element is node);return []
        node=etree.fromstring('<root/>')
        obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(Document())
        scope['xpath']=callback
        result=obj.remove_header_and_footer_references(None,node)
        return Result({'result':result,'callback':trace}, {})
    finally:
        scope['xpath']=previous_xpath
        if previous_module is not None:sys.modules['docxcompose.composer']=previous_module

def composer_renumber_bookmarks(engine):
    import importlib
    from docx import Document
    from docx.oxml import parse_xml
    w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    doc=Document();paragraph=doc.add_paragraph()._p
    for kind,number in [('Start',8),('End',7),('Start',3),('End',8),('Start',4)]:
        paragraph.append(parse_xml(f'<w:bookmark{kind} xmlns:w="{w}" w:id="{number}"/>'))
    result=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(doc).renumber_bookmarks()
    return Result({'result':result,'ids':[node.get(f'{{{w}}}id') for node in paragraph]}, {})

def composer_renumber_drawing(engine,method,tag,namespace):
    import importlib
    from docx import Document
    from docx.oxml import parse_xml
    doc=Document()
    roots=(doc.add_paragraph()._p,doc.sections[0].header.add_paragraph()._p,doc.sections[0].footer.add_paragraph()._p)
    for root in roots:root.append(parse_xml(f'<x:{tag} xmlns:x="{namespace}" id="33" name="test"/>'))
    composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(doc)
    result=getattr(composer,method)()
    return Result({'result':result,'ids':[root[-1].get('id') for root in roots]}, {})

def composer_renumber_docpr(engine):
    return composer_renumber_drawing(engine,'renumber_docpr_ids','docPr','http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing')

def composer_renumber_nvpicpr(engine):
    return composer_renumber_drawing(engine,'renumber_nvpicpr_ids','cNvPr','http://schemas.openxmlformats.org/drawingml/2006/picture')

def composer_relationship(engine,internal=False,referenced=False):
    import importlib
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from lxml import etree
    relation='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    master=Document();child=Document()
    master.part.relate_to('https://occupied.invalid',RT.HYPERLINK,is_external=True)
    if internal:
        target=Part(PackURI('/word/custom/test1.xml'),'application/xml',b'<test/>',child.part.package)
        rid=child.part.relate_to(target,'urn:test:relationship')
    else:
        rid=child.part.relate_to('https://target.invalid',RT.HYPERLINK,is_external=True)
    composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
    if referenced:
        element=etree.fromstring(f'<root xmlns:r="{relation}"><ref r:id="{rid}"/></root>')
        result=composer.add_referenced_parts(child.part,master.part,element)
        rel=master.part.rels[element[0].get(f'{{{relation}}}id')]
    else:
        rel=composer.add_relationship(child.part,master.part,child.part.rels[rid]);result=None
    return Result({'result':result,'external':rel.is_external,
                   'target':rel.target_part.blob.decode('ascii') if internal else rel.target_ref}, {})

def composer_relationship_external(engine):return composer_relationship(engine)
def composer_relationship_internal(engine):return composer_relationship(engine,internal=True)
def composer_references_external(engine):return composer_relationship(engine,referenced=True)
def composer_references_internal(engine):return composer_relationship(engine,internal=True,referenced=True)

def composer_relationship_nested(engine):
    import importlib
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    master=Document();child=Document()
    occupied=Part(PackURI('/word/custom/test1.xml'),'application/xml',b'<occupied/>',master.part.package)
    master.part.relate_to(occupied,'urn:test:occupied')
    parent=Part(PackURI('/word/custom/test1.xml'),'application/xml',b'<parent/>',child.part.package)
    nested=Part(PackURI('/word/custom/nested1.xml'),'application/xml',b'<nested/>',child.part.package)
    parent.relate_to(nested,'urn:test:nested');parent.relate_to('https://nested.invalid',RT.HYPERLINK,is_external=True)
    first=parent.rels.pop('rId1');parent.rels['rId1']=first
    rid=child.part.relate_to(parent,'urn:test:parent')
    composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
    new=composer.add_relationship(child.part,master.part,child.part.rels[rid]).target_part
    refs=[{'id':rel.rId,'external':rel.is_external,'target':rel.target_ref if rel.is_external else str(rel.target_part.partname)} for rel in new.rels.values()]
    return Result({'name':str(new.partname),'blob':new.blob.decode('ascii'),'refs':refs}, {})

def composer_relationship_invalid_nested(engine):
    import importlib
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    master=Document();child=Document()
    parent=Part(PackURI('/word/custom/test1.xml'),'application/xml',b'<parent/>',child.part.package)
    nested_id=parent.relate_to('https://nested.invalid',RT.HYPERLINK,is_external=True)
    parent.rels[nested_id]._rId='bad'
    rid=child.part.relate_to(parent,'urn:test:parent')
    composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
    try:composer.add_relationship(child.part,master.part,child.part.rels[rid]);error=None
    except Exception as exc:error={'type':type(exc).__name__,'message':str(exc)}
    created=[str(rel.target_part.partname) for rel in master.part.rels.values() if rel.reltype=='urn:test:parent']
    return Result({'error':error,'created':created}, {})

def composer_style_mapping(engine):
    import importlib
    from docx import Document
    from docx.enum.style import WD_STYLE_TYPE
    from docx.oxml.ns import qn
    master=Document();child=Document()
    a=master.styles.add_style('Shared Example',WD_STYLE_TYPE.PARAGRAPH)
    b=child.styles.add_style('Shared Example',WD_STYLE_TYPE.PARAGRAPH)
    a.element.set(qn('w:styleId'),'MasterId');b.element.set(qn('w:styleId'),'ChildId')
    obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
    result=obj._create_style_id_mapping(child)
    return Result({'result':result,'mapped':obj.mapped_style_id('ChildId'),'unknown':obj.mapped_style_id('UnknownId')}, {})

def composer_media(engine, method):
    import importlib,io
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from lxml import etree
    from PIL import Image
    r='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    a='http://schemas.openxmlformats.org/drawingml/2006/main'
    v='urn:schemas-microsoft-com:vml'
    data=io.BytesIO();Image.new('RGB',(3,2),(30,70,110)).save(data,format='PNG');raw=data.getvalue()
    master=Document();child=Document();child.add_picture(io.BytesIO(raw))
    rid=next(rel.rId for rel in child.part.rels.values() if rel.reltype==RT.IMAGE)
    external=child.part.rels.get_or_add_ext_rel(RT.IMAGE,'https://example.invalid/image.png')
    tag=f'<a:blip r:embed="{rid}" r:link="{external}"/>' if method=='add_images' else f'<v:shape><v:imagedata r:id="{rid}"/></v:shape>'
    element=etree.fromstring(f'<root xmlns:a="{a}" xmlns:v="{v}" xmlns:r="{r}">{tag}{tag}</root>')
    result=getattr(importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master),method)(child,element)
    nodes=element.xpath('.//a:blip|.//v:imagedata',namespaces={'a':a,'v':v})
    refs=[]
    for node in nodes:
        rel=master.part.rels[node.get(f'{{{r}}}'+('embed' if method=='add_images' else 'id'))]
        link=master.part.rels.get(node.get(f'{{{r}}}link'))
        refs.append({'embedded':rel.reltype==RT.IMAGE and rel.target_part.blob==raw,
                     'external':None if link is None else link.is_external and link.target_ref=='https://example.invalid/image.png'})
    return Result({'result':result,'images':sum(part.blob==raw for part in master.part.package.image_parts),'refs':refs}, {})

def composer_images(engine):return composer_media(engine,'add_images')
def composer_shapes(engine):return composer_media(engine,'add_shapes')

def composer_attach_integration(engine):
    import importlib,io
    from docx import Document
    from docx.enum.style import WD_STYLE_TYPE
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from docx.oxml import CT_SectPr,parse_xml
    from docxcompose.properties import CustomProperties
    from lxml import etree
    from PIL import Image
    w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    master=Document();master.add_paragraph('parent');child=Document()
    style=child.styles.add_style('New Example',WD_STYLE_TYPE.PARAGRAPH);style.font.bold=True
    styled=child.add_paragraph('Styled ');styled.style=style
    CustomProperties(child)['Example']='Value'
    styled._p.append(parse_xml(f'<w:fldSimple xmlns:w="{w}" w:instr=" DOCPROPERTY Example \\* MERGEFORMAT "><w:r><w:t>Value</w:t></w:r></w:fldSimple>'))
    child.add_paragraph('Numbered',style='List Number')
    part=Part(PackURI('/word/footnotes.xml'),'application/xml',f'<w:footnotes xmlns:w="{w}"><w:footnote w:id="7"><w:p><w:r><w:t>Note</w:t></w:r></w:p></w:footnote></w:footnotes>'.encode(),child.part.package)
    child.part.relate_to(part,RT.FOOTNOTES)
    note=child.add_paragraph('Body');note.add_run()._r.append(parse_xml(f'<w:footnoteReference xmlns:w="{w}" w:id="7"/>'))
    image=io.BytesIO();Image.new('RGB',(3,2),(30,70,110)).save(image,format='PNG');image.seek(0)
    child.add_paragraph('Image').add_run().add_picture(image)
    composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
    result=composer.attach_parts(child)
    dissolved=not bool(child.element.body.xpath('.//w:fldSimple'))
    for element in list(child.element.body):
        if not isinstance(element,CT_SectPr):master.element.body.insert(len(master.element.body)-1,element)
    images=sum(rel.reltype==RT.IMAGE for rel in master.part.rels.values())
    notes=etree.fromstring(master.part.rels.part_with_reltype(RT.FOOTNOTES).blob)
    note_ids=[node.get(f'{{{w}}}id') for node in notes]
    output=io.BytesIO();master.save(output)
    return Result({'result':result,'texts':[p.text for p in master.paragraphs],'style_copied':master.styles.element.get_by_id(style.style_id) is not None,
                   'images':images,'note_ids':note_ids,'property_dissolved':dissolved},{'document.docx':output.getvalue()})

def composer_restart_numbering(engine):
    import importlib,io
    from docx import Document
    master=Document();paragraph=master.add_paragraph('numbered',style='List Number')
    composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
    result=composer.restart_first_numbering(Document(),paragraph._p)
    number=paragraph._p.pPr.numPr.numId.val
    output=io.BytesIO();master.save(output)
    return Result({'result':result,'number':number,'restarted':sorted(composer._numbering_restarted)},{'document.docx':output.getvalue()})

def composer_add_footnotes(engine):
    import importlib,io
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from docx.oxml import parse_xml
    from lxml import etree
    w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    master=Document();child=Document()
    source=Part(PackURI('/word/footnotes.xml'),'application/xml',f'<w:footnotes xmlns:w="{w}"><w:footnote w:id="7"><w:p><w:r><w:t>Footnote content</w:t></w:r></w:p></w:footnote></w:footnotes>'.encode(),child.part.package)
    child.part.relate_to(source,RT.FOOTNOTES)
    paragraph=child.add_paragraph('Document body');paragraph.add_run()._r.append(parse_xml(f'<w:footnoteReference xmlns:w="{w}" w:id="7"/>'))
    element=paragraph._p;composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
    result=composer.add_footnotes(child,element)
    refs=[node.get(f'{{{w}}}id') for node in element.xpath('.//w:footnoteReference')]
    target=master.part.rels.part_with_reltype(RT.FOOTNOTES);root=etree.fromstring(target.blob)
    notes=[(node.get(f'{{{w}}}id'),''.join(node.itertext())) for node in root]
    master.element.body.insert(len(master.element.body)-1,element)
    output=io.BytesIO();master.save(output)
    return Result({'result':result,'refs':refs,'notes':notes},{'document.docx':output.getvalue()})

def composer_footnote_part(engine):
    import importlib
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from lxml import etree
    master=Document();cls=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
    composer=cls(master);part=composer.footnote_part();root=etree.fromstring(part.blob)
    return Result({'same':composer.footnote_part() is part,'linked':master.part.rels.part_with_reltype(RT.FOOTNOTES) is part,
                   'partname':str(part.partname),'content_type':part.content_type,'root':etree.QName(root).localname}, {})

def composer_add_styles(engine):
    import importlib,io
    from docx import Document
    from docx.enum.style import WD_STYLE_TYPE
    from docx.shared import Pt
    master=Document();child=Document()
    style=child.styles.add_style('New Example',WD_STYLE_TYPE.PARAGRAPH);style.font.bold=True;style.font.size=Pt(15)
    paragraph=child.add_paragraph('styled text');paragraph.style=style;element=paragraph._p
    composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master);composer._create_style_id_mapping(child)
    result=composer.add_styles(child,element)
    copied=master.styles.element.get_by_id(style.style_id)
    master.element.body.insert(len(master.element.body)-1,element)
    output=io.BytesIO();master.save(output)
    return Result({'result':result,'id':style.style_id,'copied':copied is not None,'bold':master.styles['New Example'].font.bold},{'document.docx':output.getvalue()})

def composer_add_numberings(engine):
    import importlib,io
    from unittest.mock import patch
    from docx import Document
    from docx.oxml import parse_xml
    import docxcompose.composer as composer_module
    w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    master=Document();child=Document();element=child.add_paragraph('numbered')._p
    element.get_or_add_pPr().append(parse_xml(f'<w:numPr xmlns:w="{w}"><w:numId w:val="1"/></w:numPr>'))
    composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
    with patch.object(composer_module.random,'random',lambda:0.12345678):result=composer.add_numberings(child,element)
    refs=[node.val for node in element.xpath('.//w:numId')]
    master.element.body.insert(len(master.element.body)-1,element)
    output=io.BytesIO();master.save(output)
    return Result({'result':result,'refs':refs,'num_mapping':list(composer.num_id_mapping.items()),
                   'abstract_mapping':list(composer.anum_id_mapping.items())},{'document.docx':output.getvalue()})

def composer_next_numbering_ids(engine):
    import importlib
    from types import SimpleNamespace
    from docx import Document
    from docx.oxml import parse_xml
    cls=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
    default=cls(Document())._next_numbering_ids()
    root=parse_xml('<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
    class Empty(cls):
        def numbering_part(self):return SimpleNamespace(element=root)
    empty=Empty(Document())._next_numbering_ids()
    return Result({'default':default,'empty':empty}, {})

def composer_numbering_part(engine):
    import importlib
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    cls=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
    existing=Document();original=existing.part.rels.part_with_reltype(RT.NUMBERING)
    same=cls(existing).numbering_part() is original
    missing=Document();rels=missing.part.rels;rid=next(key for key,value in rels.items() if value.reltype==RT.NUMBERING);del rels[rid]
    part=cls(missing).numbering_part()
    return Result({'existing_same':same,'new_linked':rels.part_with_reltype(RT.NUMBERING) is part,
                   'partname':str(part.partname),'content_type':part.content_type,'children':len(part.element)}, {})

def composer_numbering_helpers(engine):
    import importlib
    from types import SimpleNamespace
    from docx import Document
    from docx.oxml import parse_xml
    from lxml import etree
    w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    root=parse_xml(f'<w:numbering xmlns:w="{w}"><w:abstractNum w:abstractNumId="5"/><w:num w:numId="2"/><w:num w:numId="3"/></w:numbering>')
    base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
    class Composer(base):
        def numbering_part(self):return SimpleNamespace(element=root)
    composer=Composer(Document())
    composer._insert_num(parse_xml(f'<w:num xmlns:w="{w}" w:numId="9"/>'))
    composer._insert_abstract_num(parse_xml(f'<w:abstractNum xmlns:w="{w}" w:abstractNumId="9"/>'))
    composer.num_id_mapping={'first':7,'second':7};result=composer._replace_mapped_num_id(7,8)
    return Result({'result':result,'order':[(etree.QName(x).localname,x.get(f'{{{w}}}'+('numId' if etree.QName(x).localname=='num' else 'abstractNumId'))) for x in root],
                   'mapping':list(composer.num_id_mapping.items())}, {})

def composer_other_styles(engine):
    import importlib
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    parent=Document();child=Document();trace=[]
    part=Part(PackURI('/word/footnotes.xml'),'application/xml',b'<footnotes><style/></footnotes>',child.part.package)
    child.part.relate_to(part,RT.FOOTNOTES)
    base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
    class Composer(base):
        def add_styles(self,doc,element):trace.append({'same_doc':doc is child,'tag':element.tag,'children':len(element)})
    result=Composer(parent).add_styles_from_other_parts(child)
    return Result({'result':result,'calls':trace}, {})

def composer_style_identity(engine):
    import importlib
    from docx import Document
    key=object();name=object();value=object();trace=[]
    class IdMap:
        def __contains__(self,item):trace.append(('contains',item is key));return True
        def __getitem__(self,item):trace.append(('item',item is key));return name
    class NameMap:
        def get(self,item,default):trace.append(('get',item is name,default is key));return value
    obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(Document())
    obj._style_id2name=IdMap();obj._style_name2id=NameMap()
    result=obj.mapped_style_id(key)
    return Result({'trace':trace,'same_result':result is value}, {})


PROBES={
    'composer_attach_integration':Probe(composer_attach_integration,{'result':None,'texts':['parent','Styled Value','Numbered','Body','Image'],'style_copied':True,'images':1,'note_ids':['1'],'property_dissolved':True},True),
    'composer_restart_numbering':Probe(composer_restart_numbering,{'result':None,'number':10,'restarted':['ListNumber']},True),
    'composer_add_footnotes':Probe(composer_add_footnotes,{'result':None,'refs':['1'],'notes':[('1','Footnote content')]},True),
    'composer_footnote_part':Probe(composer_footnote_part,{'same':True,'linked':True,'partname':'/word/footnotes.xml','content_type':'application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml','root':'footnotes'},True),
    'composer_add_styles':Probe(composer_add_styles,{'result':None,'id':'NewExample','copied':True,'bold':True},True),
    'composer_add_numberings':Probe(composer_add_numberings,{'result':None,'refs':[10],'num_mapping':[(1,10)],'abstract_mapping':[(8,9)]},True),
    'composer_next_numbering_ids':Probe(composer_next_numbering_ids,{'default':(10,9),'empty':(1,0)},True),
    'composer_numbering_part':Probe(composer_numbering_part,{'existing_same':True,'new_linked':True,'partname':'/word/numbering.xml','content_type':'application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml','children':0},True),
    'composer_numbering_helpers':Probe(composer_numbering_helpers,{'result':None,'order':[('abstractNum','5'),('abstractNum','9'),('num','2'),('num','9'),('num','3')],'mapping':[('first',8),('second',7)]},True),
    'composer_other_styles':Probe(composer_other_styles,{'result':None,'calls':[{'same_doc':True,'tag':'footnotes','children':1}]},True),
    'composer_images':Probe(composer_images,{'result':None,'images':1,'refs':[{'embedded':True,'external':True}]*2},True),
    'composer_shapes':Probe(composer_shapes,{'result':None,'images':1,'refs':[{'embedded':True,'external':None}]*2},True),
    'composer_diagrams_complete':Probe(composer_diagrams_complete,{'result':None,'error':None,'transferred':[True,True,True,True]},True),
    'composer_diagrams_missing':Probe(composer_diagrams_missing,{'result':None,'error':{'type':'KeyError','args':[None]},'transferred':[True,True,False,False]},True),
    'composer_section_types':Probe(composer_section_types,{'result':None,'master':[2,2,4],'child':[2,4]},True),
    'composer_header_refs':Probe(composer_header_refs,{'result':None,'remaining':['pgSz']},True),
    'composer_header_refs_partial':Probe(composer_header_refs_partial,{'same_exception':True,'trace':['parent:first','remove:first','parent:second','remove:second']},True),
    'composer_header_refs_removed_module':Probe(composer_header_refs_removed_module,{'result':None,'callback':[True]},True),
    'composer_renumber_bookmarks':Probe(composer_renumber_bookmarks,{'result':None,'ids':['0','0','1','1','2']},True),
    'composer_renumber_docpr':Probe(composer_renumber_docpr,{'result':None,'ids':['1','2','3']},True),
    'composer_renumber_nvpicpr':Probe(composer_renumber_nvpicpr,{'result':None,'ids':['1','2','3']},True),
    'composer_relationship_external':Probe(composer_relationship_external,{'result':None,'external':True,'target':'https://target.invalid'},True),
    'composer_relationship_internal':Probe(composer_relationship_internal,{'result':None,'external':False,'target':'<test/>'},True),
    'composer_references_external':Probe(composer_references_external,{'result':None,'external':True,'target':'https://target.invalid'},True),
    'composer_references_internal':Probe(composer_references_internal,{'result':None,'external':False,'target':'<test/>'},True),
    'composer_relationship_nested':Probe(composer_relationship_nested,{'name':'/word/custom/test2.xml','blob':'<parent/>','refs':[{'id':'rId1','external':False,'target':'/word/custom/nested1.xml'},{'id':'rId2','external':True,'target':'https://nested.invalid'}]},True),
    'composer_relationship_invalid_nested':Probe(composer_relationship_invalid_nested,{'error':{'type':'AttributeError','message':"'NoneType' object has no attribute 'group'"},'created':['/word/custom/test1.xml']},True),
    'composer_style_mapping':Probe(composer_style_mapping,{'result':None,'mapped':'MasterId','unknown':'UnknownId'},True),
    'composer_style_identity':Probe(composer_style_identity,{'trace':[('contains',True),('item',True),('get',True,True)],'same_result':True},True),
}
