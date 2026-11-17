"""Reference-oracled probes for OPC graph and writer behavior."""
import io
import posixpath
import zipfile
import tempfile
from pathlib import Path
from lxml import etree

from .protocol_base import Probe, Result, generate, observed, template

CT='http://schemas.openxmlformats.org/package/2006/content-types'
REL='http://schemas.openxmlformats.org/package/2006/relationships'
BLOB=bytes(range(256))


def rewrite(source, mutate):
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        parts={name:archive.read(name) for name in archive.namelist()}
    mutate(parts)
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,blob in parts.items():archive.writestr(name,blob)
    return output.getvalue()


def encode(root):
    return etree.tostring(root,xml_declaration=True,encoding='UTF-8',standalone=True)


def add_relationship(parts, target, identity='opaque'):
    path='word/_rels/document.xml.rels'
    root=etree.fromstring(parts[path])
    etree.SubElement(root,'{'+REL+'}Relationship',Id=identity,Type='urn:docxtpl-native:probe',Target=target)
    parts[path]=encode(root)


def graph_source():
    def mutate(parts):
        root=etree.fromstring(parts['[Content_Types].xml'])
        etree.SubElement(root,'{'+CT+'}Default',Extension='unused',ContentType='application/x-unused')
        etree.SubElement(root,'{'+CT+'}Override',PartName='/unreachable/orphan.bin',ContentType='application/x-orphan')
        etree.SubElement(root,'{'+CT+'}Override',PartName='/OPAQUE/payload.DAT',ContentType='application/x-opaque')
        parts['[Content_Types].xml']=encode(root)
        parts['unreachable/orphan.bin']=b'unreachable fixture'
        parts['opaque/payload.dat']=BLOB
        add_relationship(parts,'../opaque/payload.dat')
        cycle=etree.Element('{'+REL+'}Relationships',nsmap={None:REL})
        etree.SubElement(cycle,'{'+REL+'}Relationship',Id='cycle',Type='urn:cycle',Target='../word/document.xml')
        parts['opaque/_rels/payload.dat.rels']=encode(cycle)
    return rewrite(template('{{ value }}'),mutate)


def package_graph(engine):
    blob=generate(engine,'',{'value':'X'},source=graph_source())
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        checks={'binary_unchanged':archive.read('opaque/payload.dat')==BLOB,
                'orphan_absent':'unreachable/orphan.bin' not in archive.namelist(),
                'unused_declaration_absent':b'application/x-unused' not in archive.read('[Content_Types].xml')}
    return observed(blob,**checks)


def package_paths(engine):
    external="https://example.invalid/a/../b?q=a&z='quoted'"
    def mutate(parts):
        add_relationship(parts,external,'external')
        for path in list(parts):
            if not path.endswith('.rels'):continue
            old=etree.fromstring(parts[path]);root=etree.Element('{'+REL+'}Relationships',nsmap={'alias':REL})
            base=path.rsplit('/_rels/',1)[0] if '/_rels/' in path else ''
            for child in old:
                if child.get('Id')=='external':child.set('TargetMode','External')
                if child.get('TargetMode')!='External':
                    target=posixpath.normpath(posixpath.join('/'+base,child.get('Target')))
                    child.set('Target','/./'+target.lstrip('/'));child.set('TargetMode','Internal')
                root.append(child)
            parts[path]=encode(root)
    blob=generate(engine,'',{'value':'X'},source=rewrite(template('{{ value }}'),mutate))
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        root=etree.fromstring(archive.read('word/_rels/document.xml.rels'))
        target=root.find("{*}Relationship[@Id='external']").get('Target')
    return observed(blob,external_target=target)


def package_default_choices(engine):
    def mutate(parts):
        root=etree.fromstring(parts['[Content_Types].xml'])
        for child in list(root):
            if child.get('Extension')=='jpeg':root.remove(child)
        etree.SubElement(root,'{'+CT+'}Override',PartName='/docProps/thumbnail.jpeg',ContentType='image/jpeg')
        etree.SubElement(root,'{'+CT+'}Default',Extension='ODD',ContentType='application/x-opaque')
        etree.SubElement(root,'{'+CT+'}Override',PartName='/opaque/payload.XML',ContentType='application/x-custom-xml')
        parts['[Content_Types].xml']=encode(root)
        parts['opaque/payload.odd']=BLOB
        parts['opaque/payload.XML']=b'<opaque>preserve this generic XML blob</opaque>'
        parts['opaque/_rels/payload.odd.rels']=encode(etree.Element('{'+REL+'}Relationships',nsmap={None:REL}))
        add_relationship(parts,'../opaque/payload.odd','odd');add_relationship(parts,'../opaque/payload.XML','xml')
    blob=generate(engine,'',{'value':'X'},source=rewrite(template('{{ value }}'),mutate))
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        root=etree.fromstring(archive.read('[Content_Types].xml'))
        defaults={c.get('Extension'):c.get('ContentType') for c in root if c.tag=='{'+CT+'}Default'}
        overrides={c.get('PartName'):c.get('ContentType') for c in root if c.tag=='{'+CT+'}Override'}
        checks={'jpeg_default':defaults.get('jpeg'),'opaque_override':overrides.get('/opaque/payload.odd'),
                'custom_xml_override':overrides.get('/opaque/payload.XML'),
                'odd_default_absent':'odd' not in defaults and 'ODD' not in defaults,
                'empty_relationships_absent':'opaque/_rels/payload.odd.rels' not in archive.namelist()}
    return observed(blob,**checks)


def package_save_only(engine):
    document=engine.DocxTemplate(io.BytesIO(graph_source()))
    outputs={}
    for name in ('first.docx','second.docx'):
        output=io.BytesIO();document.save(output);outputs[name]=output.getvalue()
    result=observed(outputs['first.docx'],is_saved=document.is_saved,is_rendered=document.is_rendered)
    return Result(result.observation,outputs)


def package_post_replacement(engine):
    source=template('{{ value }}')
    with zipfile.ZipFile(io.BytesIO(source)) as archive:root=etree.fromstring(archive.read('_rels/.rels'))
    for child in root:
        if child.get('TargetMode')!='External':child.set('TargetMode','Internal')
    replacement=encode(root)
    document=engine.DocxTemplate(io.BytesIO(source))
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/'relationships.xml';path.write_bytes(replacement)
        document.replace_zipname('_rels/.rels',str(path))
        document.render({'value':'X'})
        output=io.BytesIO();document.save(output);blob=output.getvalue()
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:unchanged=archive.read('_rels/.rels')==replacement
    return observed(blob,replacement_verbatim=unchanged)


PROBES={
    'package_graph':Probe(package_graph,{'text':'X','binary_unchanged':True,'orphan_absent':True,'unused_declaration_absent':True}),
    'package_paths':Probe(package_paths,{'text':'X','external_target':"https://example.invalid/a/../b?q=a&z='quoted'"}),
    'package_default_choices':Probe(package_default_choices,{'text':'X','jpeg_default':'image/jpeg','opaque_override':'application/x-opaque',
        'custom_xml_override':'application/x-custom-xml','odd_default_absent':True,'empty_relationships_absent':True}),
    'package_save_only':Probe(package_save_only,{'text':'{{ value }}','is_saved':True,'is_rendered':False}),
    'package_post_replacement':Probe(package_post_replacement,{'text':'X','replacement_verbatim':True}),
}
