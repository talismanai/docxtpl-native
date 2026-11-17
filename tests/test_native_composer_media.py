"""Compare composition of images and VML shapes against docxtpl."""
import importlib
import io
import unittest
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from lxml import etree
from PIL import Image
import docxtpl
import docxtpl_native

R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
A='http://schemas.openxmlformats.org/drawingml/2006/main'
V='urn:schemas-microsoft-com:vml'
W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'

def observe(engine, method, existing=False, link=False, missing=False):
    parent=Document();child=Document()
    data=io.BytesIO();Image.new('RGB',(3,2),(30,70,110)).save(data,format='PNG');raw=data.getvalue()
    if existing:parent.add_picture(io.BytesIO(raw))
    child.add_picture(io.BytesIO(raw))
    rid=next(rel.rId for rel in child.part.rels.values() if rel.reltype==RT.IMAGE)
    if missing:rid='rId999'
    external=child.part.rels.get_or_add_ext_rel(RT.IMAGE,'https://example.invalid/image.png')
    tag=f'<a:blip r:embed="{rid}"'+(f' r:link="{external}"' if link else '')+'/>' if method=='add_images' else f'<v:shape><v:imagedata r:id="{rid}"/></v:shape>'
    element=etree.fromstring(f'<w:p xmlns:w="{W}" xmlns:a="{A}" xmlns:v="{V}" xmlns:r="{R}">{tag}{tag}</w:p>')
    composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(parent)
    try:result=getattr(composer,method)(child,element);error=None
    except Exception as exc:result=None;error=(type(exc).__name__,exc.args)
    nodes=element.xpath('.//a:blip|.//v:imagedata',namespaces={'a':A,'v':V})
    attrs=[]
    for node in nodes:
        copied=parent.part.rels.get(node.get(f'{{{R}}}'+('embed' if method=='add_images' else 'id')))
        linked=parent.part.rels.get(node.get(f'{{{R}}}link'))
        attrs.append((None if copied is None else (copied.reltype,copied.target_part.blob==raw),None if linked is None else (linked.reltype,linked.target_ref,linked.is_external)))
    images=[part for part in parent.part.package.image_parts if part.blob==raw]
    return result,error,attrs,len(images),etree.tostring(element)

class NativeComposerMediaTests(unittest.TestCase):
    def test_real_images_and_shapes_deduplicate(self):
        for method in ('add_images','add_shapes'):
            for existing in (False,True):
                with self.subTest(method=method,existing=existing):
                    actual=observe(docxtpl_native,method,existing=existing)
                    self.assertEqual(actual,observe(docxtpl,method,existing=existing))
                    self.assertEqual(actual[3],1)

    def test_embedded_image_external_link(self):
        actual=observe(docxtpl_native,'add_images',link=True)
        self.assertEqual(actual,observe(docxtpl,'add_images',link=True))
        self.assertTrue(all(item[1][2] for item in actual[2]))

    def test_missing_relationship_preserves_failure(self):
        for method in ('add_images','add_shapes'):
            with self.subTest(method=method):
                self.assertEqual(observe(docxtpl_native,method,missing=True),observe(docxtpl,method,missing=True))

    def test_user_image_wrapper_is_called_only_for_new_media(self):
        import docxcompose.composer as composer_module
        from unittest.mock import patch
        original=composer_module.ImageWrapper
        def run(engine,existing):
            seen=[]
            def wrapper(part):
                seen.append((part.filename,part.content_type));return original(part)
            with patch.object(composer_module,'ImageWrapper',wrapper):
                result=observe(engine,'add_images',existing=existing)
            return seen,result
        for existing in (False,True):
            with self.subTest(existing=existing):
                actual=run(docxtpl_native,existing)
                self.assertEqual(actual,run(docxtpl,existing))
                self.assertEqual(len(actual[0]),0 if existing else 1)

    def test_image_lookup_resolves_method_before_sha1(self):
        def run(engine):
            trace=[];destination_image=object()
            class ImagePart:
                @property
                def sha1(self):trace.append('sha1');return 'hash'
            class Parts:
                def __getattribute__(self,name):
                    if name=='_get_by_sha1':trace.append('lookup')
                    return object.__getattribute__(self,name)
                def _get_by_sha1(self,value):trace.append(('call',value));return destination_image
            class Package:image_parts=Parts()
            class MasterPart:
                package=Package()
                def relate_to(self,image,kind):return 'new'
            class Master:part=MasterPart()
            class Rel:target_part=ImagePart()
            class ChildPart:rels={'old':Rel()}
            class Child:part=ChildPart()
            class Node:
                def get(self,name):return None if name.endswith('link') else 'old'
                def set(self,name,value):pass
            class Element:
                def xpath(self,*args,**kwargs):return [Node()]
            composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(Master())
            composer.add_images(Child(),Element())
            return trace
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_external_relationship_resolves_callback_before_destination(self):
        from types import SimpleNamespace
        def run(engine):
            trace=[]
            class Parts:
                def _get_by_sha1(self,value):return object()
            class Package:image_parts=Parts()
            class MasterPart:
                package=Package()
                def relate_to(self,image,kind):return 'new'
            class Master:part=MasterPart()
            class ChildPart:
                rels={'old':SimpleNamespace(target_part=SimpleNamespace(sha1='hash')),'external':object()}
            class Child:part=ChildPart()
            class Node:
                def get(self,name):
                    if name.endswith('link'):trace.clear();return 'external'
                    return 'old'
                def set(self,name,value):pass
            class Element:
                def xpath(self,*args,**kwargs):return [Node()]
            base=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            class Composer(base):
                def __getattribute__(self,name):
                    if name in ('add_relationship','doc'):trace.append(name)
                    return super().__getattribute__(name)
                def add_relationship(self,src,dst,rel):return SimpleNamespace(rId='external-new')
            Composer(Master()).add_images(Child(),Element())
            return trace
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_monkeypatched_wrapper_initializer_is_called(self):
        import docxcompose.composer as composer_module
        from unittest.mock import patch
        cls=composer_module.ImageWrapper;original=cls.__init__
        def run(engine):
            trace=[]
            def init(instance,part):trace.append(type(instance) is cls);original(instance,part)
            with patch.object(cls,'__init__',init):result=observe(engine,'add_images')
            return trace,result
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_initializer_with_reference_names_still_runs(self):
        import docxcompose.image as image_module
        from types import FunctionType
        from unittest.mock import patch
        cls=image_module.ImageWrapper
        def altered(self,img_part):
            self.sha1=img_part.sha1
            self.filename=img_part.filename
            self.ext=os.path.splitext(self.filename)[1][1:]
            self.content_type=img_part.content_type
            self.blob=img_part.blob+b'altered'
        altered_code=altered.__code__.replace(co_name='__init__')
        self.assertEqual(altered_code.co_names,cls.__init__.__code__.co_names)
        altered_init=FunctionType(altered_code,image_module.__dict__,'__init__')
        def run(engine):
            with patch.object(cls,'__init__',altered_init):return observe(engine,'add_images')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_new_wrapper_identity_fields_and_splitext_hook(self):
        import docxcompose.image as image_module
        from types import SimpleNamespace
        from unittest.mock import patch
        original=image_module.os.path.splitext
        def run(engine,filename):
            trace=[];captured=[]
            class Parts:
                def _get_by_sha1(self,value):return None
                def _add_image_part(self,wrapper):
                    captured.append((type(wrapper) is image_module.ImageWrapper,dict(wrapper.__dict__)))
                    return object()
            class Package:image_parts=Parts()
            class MasterPart:
                package=Package()
                def relate_to(self,image,kind):return 'new'
            class Master:part=MasterPart()
            part=SimpleNamespace(sha1='hash',filename=filename,content_type='image/test',blob=b'blob')
            class ChildPart:rels={'old':SimpleNamespace(target_part=part)}
            class Child:part=ChildPart()
            class Node:
                def get(self,name):return None if name.endswith('link') else 'old'
                def set(self,name,value):pass
            class Element:
                def xpath(self,*args,**kwargs):return [Node()]
            def splitext(value):trace.append(value);return original(value)
            composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(Master())
            with patch.object(image_module.os.path,'splitext',splitext):composer.add_images(Child(),Element())
            return trace,captured
        for filename in ('plain','.hidden','dir/.hidden.png','name.','name\udcff.png',b'name.bin'):
            with self.subTest(filename=filename):
                self.assertEqual(run(docxtpl_native,filename),run(docxtpl,filename))

if __name__=='__main__':unittest.main()
