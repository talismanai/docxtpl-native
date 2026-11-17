"""Public docxtpl API observations with independent, assertion-only oracles."""
import io
import tempfile
from pathlib import Path
from lxml import etree
from .protocol_base import Probe,Result,template


def constructor_lazy(engine):
    class Source:
        def read(self,*args):raise AssertionError('constructor read the source')
        def seek(self,*args):raise AssertionError('constructor sought the source')
    source=Source();doc=engine.DocxTemplate(source)
    return Result({'same_source':doc.template_file is source,'loaded':doc.docx is not None,
                   'rendered':doc.is_rendered,'saved':doc.is_saved},{})


def reset_replacements(engine):
    doc=engine.DocxTemplate(io.BytesIO(template('X')))
    names=['crc_to_new_media','crc_to_new_embedded','zipname_to_replace','pics_to_replace']
    old=[]
    for name in names:
        mapping=getattr(doc,name);mapping['sentinel']=b'X';old.append(mapping)
    doc.reset_replacements()
    return Result({'empty':[getattr(doc,n)=={} for n in names],
                   'new_maps':[getattr(doc,n) is not previous for n,previous in zip(names,old)]},{})


def xml_serialization(engine):
    doc=engine.DocxTemplate(io.BytesIO(template('X')))
    xml=etree.fromstring(b'<p xmlns="urn:test"> X </p>')
    # The pinned reference ignores its encoding argument and always returns text.
    value=doc.xml_to_string(xml,encoding='UTF-8')
    return Result({'value':value,'type':type(value).__name__},{})


def get_xml(engine):
    doc=engine.DocxTemplate(io.BytesIO(template('Hello {{ value }}')));doc.get_docx()
    xml=etree.fromstring(doc.get_xml().encode('utf-8'))
    return Result({'root':etree.QName(xml).localname,'text':xml.xpath('string(.)')},{})


def write_xml(engine):
    doc=engine.DocxTemplate(io.BytesIO(template('Hello {{ value }}')));doc.get_docx()
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/'body.xml';doc.write_xml(str(path));xml=etree.fromstring(path.read_bytes())
    return Result({'root':etree.QName(xml).localname,'text':xml.xpath('string(.)')},{})

PROBES={
 'api_constructor_lazy':Probe(constructor_lazy,{'same_source':True,'loaded':False,'rendered':False,'saved':False},True),
 'api_reset_replacements':Probe(reset_replacements,{'empty':[True]*4,'new_maps':[True]*4},True),
 'api_xml_serialization':Probe(xml_serialization,{'value':'<p xmlns="urn:test"> X </p>','type':'str'},True),
 'api_get_xml':Probe(get_xml,{'root':'body','text':'Hello {{ value }}'},True),
 'api_write_xml':Probe(write_xml,{'root':'body','text':'Hello {{ value }}'},True),
}


def document_api_callbacks(engine):
    import importlib
    from types import SimpleNamespace
    from unittest.mock import patch
    module=importlib.import_module('docxtpl.template' if engine.__name__=='docxtpl' else 'docxtpl_native')
    trace=[]; body=object()
    class Subject:
        def __init__(self):object.__setattr__(self,'docx',SimpleNamespace(_element=SimpleNamespace(body=body)))
        def __setattr__(self,name,value):trace.append('set:'+name);object.__setattr__(self,name,value)
        def init_docx(self):trace.append('init')
        def xml_to_string(self,value):assert value is body;trace.append('serialize');return 'xml'
        def get_xml(self):trace.append('get_xml');return engine.DocxTemplate.get_xml(self)
    class File:
        def write(self,value):assert value=='xml';trace.append('write')
    class Manager:
        def __enter__(self):trace.append('enter');return File()
        def __exit__(self,*args):trace.append('exit')
    def opener(filename,mode):assert filename=='target' and mode=='w';trace.append('open');return Manager()
    subject=Subject();engine.DocxTemplate.render_init(subject)
    xml=engine.DocxTemplate.get_xml(subject)
    with patch.object(module,'open',opener,create=True):engine.DocxTemplate.write_xml(subject,'target')
    return Result({'trace':trace,'xml':xml,'state':[subject.pic_map=={},subject.current_rendering_part,
                                                   subject.docx_ids_index,subject.is_saved]}, {})


PROBES['api_document_callbacks']=Probe(document_api_callbacks,
    {'trace':['init','set:pic_map','set:current_rendering_part','set:docx_ids_index','set:is_saved',
              'serialize','open','enter','get_xml','serialize','write','exit'],
     'xml':'xml','state':[True,None,1000,False]}, True)


def crc_current_position(engine):
    source=io.BytesIO(b'prefix123456789');source.seek(6)
    value=engine.DocxTemplate.get_file_crc(source)
    return Result({'crc':value,'position':source.tell()},{})


def crc_read_only(engine):
    class Source:
        calls=0
        def read(self):self.calls+=1;return b'123456789'
    source=Source();value=engine.DocxTemplate.get_file_crc(source)
    return Result({'crc':value,'calls':source.calls},{})

PROBES['api_crc_current_position']=Probe(crc_current_position,{'crc':3421780262,'position':15},True)
PROBES['api_crc_read_only']=Probe(crc_read_only,{'crc':3421780262,'calls':1},True)


def crc_buffer(kind):
    def execute(engine):
        data=bytearray(b'123456789') if kind=='bytearray' else memoryview(b'123456789')
        class Source:
            def read(self):return data
        return Result({'crc':engine.DocxTemplate.get_file_crc(Source())},{})
    return execute

PROBES['api_crc_bytearray']=Probe(crc_buffer('bytearray'),{'crc':3421780262},True)
PROBES['api_crc_memoryview']=Probe(crc_buffer('memoryview'),{'crc':3421780262},True)


def crc_noncontiguous(engine):
    class Source:
        def read(self):return memoryview(b'123456789')[::2]
    try:engine.DocxTemplate.get_file_crc(Source())
    except BufferError as error:return Result({'type':'builtins.BufferError','message':str(error)},{})
    raise AssertionError('noncontiguous CRC buffer must be rejected')
PROBES['api_crc_noncontiguous']=Probe(crc_noncontiguous,{'type':'builtins.BufferError','message':'memoryview: underlying buffer is not C-contiguous'},True)


def crc_invalid(value):
    def execute(engine):
        class Source:
            calls=0
            def read(self):self.calls+=1;return value
        source=Source()
        try:engine.DocxTemplate.get_file_crc(source)
        except TypeError as error:return Result({'type':'builtins.TypeError','message':str(error),'read_calls':source.calls},{})
        raise AssertionError('non-buffer CRC input must fail')
    return execute
for label,value,message in [('text','123','str'),('none',None,'NoneType'),('integer',3,'int')]:
    PROBES['api_crc_invalid_'+label]=Probe(crc_invalid(value),{'type':'builtins.TypeError','message':"a bytes-like object is required, not '"+message+"'",'read_calls':1},True)


def replace_media_streams(engine):
    trace=[]
    class Stream(io.BytesIO):
        def __init__(self,data,label):super().__init__(data);self.label=label
        def read(self,*args):trace.append(self.label);return super().read(*args)
    source=Stream(b'prefix123456789','source');source.seek(6)
    target=Stream(b'prefixNEW','target');target.seek(6)
    doc=engine.DocxTemplate(io.BytesIO(template('X')));doc.replace_media(source,target)
    return Result({'trace':trace,'source_position':source.tell(),'target_position':target.tell(),
                   'bytes':list(doc.crc_to_new_media[3421780262])},{})


def replace_picture_stream(engine):
    target=io.BytesIO(b'prefixNEW');target.seek(6)
    doc=engine.DocxTemplate(io.BytesIO(template('X')));doc.replace_pic('image.png',target)
    return Result({'position':target.tell(),'bytes':list(doc.pics_to_replace['image.png'])},{})


def replacement_requires_path(method):
    def execute(engine):
        class Source:
            calls=0
            def read(self):self.calls+=1;return b'123456789'
        source=Source();target=io.BytesIO(b'NEW');doc=engine.DocxTemplate(io.BytesIO(template('X')))
        try:
            getattr(doc,method)(source if method=='replace_embedded' else 'word/item.bin',target)
        except TypeError as error:
            return Result({'type':'builtins.TypeError','message':str(error),'source_reads':source.calls},{})
        raise AssertionError('reference replacement requires a destination path')
    return execute

PROBES['api_replace_media_streams']=Probe(replace_media_streams,{'trace':['source','target'],'source_position':15,'target_position':9,'bytes':[78,69,87]},True)
PROBES['api_replace_picture_stream']=Probe(replace_picture_stream,{'position':9,'bytes':[78,69,87]},True)
for method in ['replace_embedded','replace_zipname']:
    PROBES['api_'+method+'_path_only']=Probe(replacement_requires_path(method),{'type':'builtins.TypeError','message':'expected str, bytes or os.PathLike object, not BytesIO','source_reads':0},True)


def replacement_callbacks(method):
    def execute(engine):
        import importlib
        from unittest.mock import patch
        module=importlib.import_module('docxtpl.template' if engine.__name__=='docxtpl' else 'docxtpl_native')
        trace=[]; stored={}; source=object(); destination=object()
        mapping={'replace_media':'crc_to_new_media','replace_pic':'pics_to_replace',
                 'replace_embedded':'crc_to_new_embedded','replace_zipname':'zipname_to_replace'}[method]
        class Mapping:
            def __setitem__(self,key,value):trace.append('set');stored['value']=value.decode()
        class File:
            def read(self):trace.append('read');return b'new'
        class Manager:
            def __enter__(self):trace.append('enter');return File()
            def __exit__(self,*args):trace.append('exit')
        def opener(value,mode):
            assert value is destination and mode=='rb'
            trace.append('open');return Manager()
        tpl=engine.DocxTemplate(None)
        tpl.get_file_crc=lambda value:trace.append('crc') or 7
        setattr(tpl,mapping,Mapping())
        with patch.object(module,'open',opener,create=True):
            result=getattr(tpl,method)(source if method in ('replace_media','replace_embedded') else 'item',destination)
        return Result({'result':result,'trace':trace,'value':stored.get('value')},{})
    return execute


for method,trace in {
    'replace_media':['crc','open','enter','read','set','exit'],
    'replace_pic':['open','enter','read','set','exit'],
    'replace_embedded':['open','enter','crc','read','set','exit'],
    'replace_zipname':['open','enter','read','set','exit'],
}.items():
    PROBES['api_'+method+'_callbacks']=Probe(replacement_callbacks(method),
                                             {'result':None,'trace':trace,'value':'new'},True)


def postprocess_no_replacements(engine):
    trace=[]
    class Source:
        def read(self,*args): trace.append('read'); raise AssertionError('unexpected read')
        def seek(self,*args): trace.append('seek'); raise AssertionError('unexpected seek')
    tpl=engine.DocxTemplate(None)
    return Result({'result':tpl.post_processing(Source()),'trace':trace},{})


def postprocess_stream_replacements(engine):
    from PIL import Image
    from docx import Document
    import zipfile,zlib
    def png(color):
        out=io.BytesIO(); Image.new('RGB',(3,3),color).save(out,format='PNG'); return out.getvalue()
    original,by_name,by_crc=map(png,('red','blue','yellow'))
    doc=Document(); doc.add_paragraph('body'); doc.add_paragraph().add_run().add_picture(io.BytesIO(original))
    stream=io.BytesIO(); doc.save(stream); stream.seek(7)
    tpl=engine.DocxTemplate(None); tpl.zipname_to_replace={'word/media/image1.png':by_name}
    tpl.crc_to_new_media={zlib.crc32(original):by_crc}
    result=tpl.post_processing(stream); position=stream.tell(); output=stream.getvalue()
    with zipfile.ZipFile(io.BytesIO(output)) as archive:
        item=archive.getinfo('word/media/image1.png'); media=archive.read(item)
    return Result({'result':result,'position':position,'name_precedes_crc':media==by_name,'compression':item.compress_type}, {'document.docx':output})


def postprocess_directory_path(engine):
    import zipfile
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w') as out:
        out.writestr('word/media/',b'')
        out.writestr('word/media/a.png',b'old',compress_type=zipfile.ZIP_DEFLATED)
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'input.zip';path.write_bytes(archive.getvalue())
        tpl=engine.DocxTemplate(None);tpl.zipname_to_replace={'word/media/':b'replacement'}
        result=tpl.post_processing(path)
        with zipfile.ZipFile(path) as src:
            entries=[[item.filename,item.is_dir(),item.compress_type,src.read(item.filename).decode()] for item in src.infolist()]
        return Result({'result':result,'entries':entries,'temporary_exists':Path(str(path)+'_docxtpl_before_replace_medias').exists()}, {})


def postprocess_invalid_path(engine):
    import zipfile
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'input.zip'; path.write_bytes(b'invalid')
        tpl=engine.DocxTemplate(None); tpl.zipname_to_replace={'word/media/a.png':b'new'}
        try: tpl.post_processing(path)
        except zipfile.BadZipFile as error:
            temporary=Path(str(path)+'_docxtpl_before_replace_medias')
            return Result({'type':type(error).__name__,'message':str(error),'original_exists':path.exists(),
                'temporary_exists':temporary.exists(),'temporary_contents':temporary.read_bytes().decode() if temporary.exists() else None}, {})
    raise AssertionError('invalid ZIP must fail')


def postprocess_non_docx_stream(engine):
    import zipfile
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w') as out:
        out.writestr('other',b'data');out.comment=b'comment'
    source=archive.getvalue();stream=io.BytesIO(source);stream.seek(2)
    tpl=engine.DocxTemplate(None);tpl.zipname_to_replace={'unused':b'x'}
    try: tpl.post_processing(stream)
    except KeyError as error:
        return Result({'type':type(error).__name__,'message':str(error),
                       'position_at_directory_end':stream.tell()==source.rfind(b'PK\x05\x06'),
                       'unchanged':stream.getvalue()==source},{})
    raise AssertionError('ZIP without content types must fail')


PROBES['api_postprocess_no_replacements']=Probe(postprocess_no_replacements,{'result':None,'trace':[]},True)
PROBES['api_postprocess_stream_replacements']=Probe(postprocess_stream_replacements,{'result':None,'position':0,'name_precedes_crc':True,'compression':8})
PROBES['api_postprocess_directory_path']=Probe(postprocess_directory_path,{'result':None,'entries':[['word/media/',True,0,'replacement'],['word/media/a.png',False,8,'old']],'temporary_exists':False},True)
PROBES['api_postprocess_invalid_path']=Probe(postprocess_invalid_path,{'type':'BadZipFile','message':'File is not a zip file','original_exists':False,'temporary_exists':True,'temporary_contents':'invalid'},True)
PROBES['api_postprocess_non_docx_stream']=Probe(postprocess_non_docx_stream,{'type':'KeyError','message':'"There is no item named \'[Content_Types].xml\' in the archive"','position_at_directory_end':True,'unchanged':True},True)
