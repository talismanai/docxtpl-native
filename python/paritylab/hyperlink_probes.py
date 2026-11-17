"""Pre-render relationship IDs, reuse and save/reload boundaries."""
import io
import zipfile
from lxml import etree
from .protocol_base import Probe, Result, template, observed
from .opc_probes import rewrite,REL,encode

HYPERLINK='http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink'

def targets(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as package:
        root=etree.fromstring(package.read('word/_rels/document.xml.rels'))
        return sorted(item.get('Target') for item in root if item.get('Type')==HYPERLINK)

def save(document):
    output=io.BytesIO();document.save(output);return output.getvalue()

def hyperlink_ids(engine):
    def mutate(parts):
        root=etree.fromstring(parts['word/_rels/document.xml.rels'])
        etree.SubElement(root,'{'+REL+'}Relationship',Id='rId10',Type=HYPERLINK,Target='https://example.test/reserved',TargetMode='External')
        parts['word/_rels/document.xml.rels']=encode(root)
    document=engine.DocxTemplate(io.BytesIO(rewrite(template('{{r rich }}'),mutate)))
    url="https://example.test/façade?q=a&quoted='yes'"
    ids=[document.build_url_id(url),document.build_url_id(url),document.build_url_id('https://example.test/two')]
    rich=engine.RichText('one',url_id=ids[0]);rich.add('|');rich.add('two',url_id=ids[2])
    document.render({'rich':rich});output=save(document)
    result=observed(output,ids=ids,targets=targets(output));return result

def hyperlink_save_lifecycle(engine):
    document=engine.DocxTemplate(io.BytesIO(template('body')))
    first=document.build_url_id('https://example.test/discarded')
    outputs={'first.docx':save(document)}
    second=document.build_url_id('https://example.test/retained')
    outputs['second.docx']=save(document)
    document.render({});outputs['rendered.docx']=save(document)
    document.render({});outputs['reloaded.docx']=save(document)
    return Result({'ids':[first,second],'targets':[targets(blob) for blob in outputs.values()],
                   'flags':[document.is_rendered,document.is_saved]},outputs)

def hyperlink_worker_thread(engine):
    from concurrent.futures import ThreadPoolExecutor
    document=engine.DocxTemplate(io.BytesIO(template('{{r rich }}')))
    identity=document.build_url_id('https://example.test/worker')
    rich=engine.RichText('worker',url_id=identity)
    def generate_in_worker():document.render({'rich':rich});return save(document)
    with ThreadPoolExecutor(max_workers=1) as executor:output=executor.submit(generate_in_worker).result()
    return observed(output,targets=targets(output))

PROBES={
    'hyperlink_ids':Probe(hyperlink_ids,{'text':'one|two','ids':['rId9','rId9','rId11'],
        'targets':["https://example.test/façade?q=a&quoted='yes'",'https://example.test/reserved','https://example.test/two']}),
    'hyperlink_save_lifecycle':Probe(hyperlink_save_lifecycle,{'ids':['rId9','rId9'],
        'targets':[[],['https://example.test/retained'],['https://example.test/retained'],[]],'flags':[True,True]}),
    'hyperlink_worker_thread':Probe(hyperlink_worker_thread,{'text':'worker','targets':['https://example.test/worker']}),
}
