"""Live header/footer traversal preserves Python protocols and partial commits."""
import unittest
from unittest.mock import patch

import docxtpl_native

ENCODING='utf-8'


def reference(document,render_xml):
    for uri in (document.HEADER_URI,document.FOOTER_URI):
        for relationship in document.docx._part.rels.values():
            if relationship.reltype!=uri or not relationship.target_part.blob:continue
            old=relationship.target_part
            source=document.xml_to_string(parse_xml(old.blob))
            xml=render_xml(source,old).encode('utf-8')
            new=XmlPart.load(old.partname,old.content_type,xml,old.package)
            for rel in old.rels.values():new.load_rel(rel.reltype,rel._target,rel.rId,rel.is_external)
            relationship._target=new


class NativeLiveHeadersTests(unittest.TestCase):
    def observe(self,method,*,fail=None,mutate_class=False):
        events=[];failure=RuntimeError('original failure')
        class Flag:
            def __init__(self,value):self.value=value
            def __bool__(self):events.append(('compare_bool',self.value));return self.value
        class Kind(str):
            def __ne__(self,other):
                events.append(('compare',str(self),str(other)))
                return Flag(super().__ne__(other))
        class InnerRelation:
            def __init__(self,label):self.label=label
            def __getattribute__(self,name):
                if name in ('reltype','_target','rId','is_external'):
                    label=object.__getattribute__(self,'label');events.append(('inner_attr',label,name))
                    return {'reltype':'inner','_target':'target','rId':label,'is_external':False}[name]
                return object.__getattribute__(self,name)
        class Relationships:
            def __init__(self,label,items):self.label=label;self.items=items
            def values(self):events.append(('values',self.label));return iter(self.items)
        class OldPart:
            def __init__(self,label,blob):self.label=label;self.data=blob
            @property
            def blob(self):events.append(('blob',self.label));return self.data
            @property
            def partname(self):events.append(('partname',self.label));return '/word/'+self.label+'.xml'
            @property
            def content_type(self):events.append(('content_type',self.label));return 'application/xml'
            @property
            def package(self):events.append(('package',self.label));return 'package'
            @property
            def rels(self):events.append(('rels',self.label));return Relationships(self.label,[InnerRelation(self.label+'-id')])
        class NewPart:
            def __init__(self,label,kind):self.label=label;self.kind=kind;self.loaded=[]
            def __getattribute__(self,name):
                if name=='load_rel':events.append(('lookup_load_rel',object.__getattribute__(self,'label')))
                return object.__getattribute__(self,name)
            def load_rel(self,kind,target,id,external):
                events.append(('load_rel',self.label,kind,target,id,external))
                if fail=='copy:'+self.label:raise failure
                self.loaded.append((kind,target,id,external))
        class Rel:
            def __init__(self,label,kind,blob):
                object.__setattr__(self,'label',label);object.__setattr__(self,'kind',Kind(kind))
                object.__setattr__(self,'_target',OldPart(label,blob))
            @property
            def reltype(self):events.append(('reltype',self.label));return self.kind
            @property
            def target_part(self):events.append(('target',self.label));return self._target
            def __setattr__(self,name,value):
                if name=='_target':events.append(('commit',self.label,value.label))
                object.__setattr__(self,name,value)
        relationships=[Rel('header','H',b'header'),Rel('empty','H',b''),Rel('footer','F',b'footer')]
        class MainPart:
            @property
            def rels(self):events.append('main_rels');return Relationships('main',relationships)
        class Model:
            @property
            def _part(self):events.append('main_part');return MainPart()
        class Document:
            HEADER_URI='H';FOOTER_URI='F'
            def __getattribute__(self,name):
                if name=='xml_to_string':events.append('lookup_serialize')
                if name in ('HEADER_URI','FOOTER_URI'):events.append(('uri',name))
                return object.__getattribute__(self,name)
            @property
            def docx(self):events.append('docx');return Model()
            def xml_to_string(self,value):events.append(('serialize',value));return 'source:'+value
        class Rendered(str):
            def encode(self,encoding):events.append(('encode',encoding));return super().encode(encoding)
        class FactoryMeta(type):
            def __getattribute__(cls,name):
                if name=='load':events.append(('lookup_load',type.__getattribute__(cls,'__name__')))
                return type.__getattribute__(cls,name)
        class Factory(metaclass=FactoryMeta):
            @staticmethod
            def load(name,content,xml,package):
                events.append(('load','normal',name,content,xml,package));return NewPart(name,'normal')
        class Alternative(metaclass=FactoryMeta):
            @staticmethod
            def load(name,content,xml,package):
                events.append(('load','alternate',name,content,xml,package));return NewPart(name,'alternate')
        def parse(blob):events.append(('parse',blob));return blob.decode()
        def render(source,part):
            events.append(('render',part.label,source))
            if fail=='render:'+part.label:raise failure
            if mutate_class and part.label=='header':globals()['XmlPart']=Alternative
            return Rendered(source+'!')
        with patch.dict(globals(),{'parse_xml':parse,'XmlPart':Factory}):
            try:result=method(Document(),render);outcome=('ok',result)
            except Exception as error:outcome=('error',type(error),str(error),error is failure)
        state=[(rel.label,type(rel._target).__name__,getattr(rel._target,'kind',None)) for rel in relationships]
        return outcome,events,state

    def native(self,document,render):
        return docxtpl_native._extension().render_live_headers(document,render,ENCODING)

    def test_order_relationships_and_partial_failure(self):
        for options in ({},{'fail':'render:footer'},{'fail':'copy:/word/header.xml'},{'mutate_class':True}):
            with self.subTest(**options):self.assertEqual(self.observe(self.native,**options),self.observe(reference,**options))


if __name__=='__main__':unittest.main()
