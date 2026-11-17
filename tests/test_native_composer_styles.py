"""Style-name mapping and exposed mapping protocols."""
import importlib
import unittest

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn
import docxtpl
import docxtpl_native


class NativeComposerStyleTests(unittest.TestCase):
    def test_real_style_name_mapping(self):
        def run(engine):
            master=Document();child=Document()
            master_style=master.styles.add_style('Shared Example',WD_STYLE_TYPE.PARAGRAPH)
            child_style=child.styles.add_style('Shared Example',WD_STYLE_TYPE.PARAGRAPH)
            master_style.element.set(qn('w:styleId'),'MasterId');child_style.element.set(qn('w:styleId'),'ChildId')
            composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            result=composer._create_style_id_mapping(child)
            return result,composer.mapped_style_id('ChildId'),composer.mapped_style_id('UnknownId')
        actual=run(docxtpl_native)
        self.assertEqual(actual,run(docxtpl));self.assertEqual(actual,(None,'MasterId','UnknownId'))

    def test_mapping_creation_order_and_partial_failure(self):
        def run(engine,fail):
            trace=[];marker=RuntimeError('master style failed')
            class Style:
                def __init__(self,label):self.label=label
                @property
                def style_id(self):trace.append((self.label,'id'));return self.label+'-id'
                @property
                def name(self):
                    trace.append((self.label,'name'))
                    if fail and self.label=='master':raise marker
                    return self.label+'-name'
            class Part:package=object()
            class Doc:
                part=Part()
                def __init__(self,label):self.label=label
                @property
                def styles(self):trace.append((self.label,'styles'));return [Style(self.label)]
            master=Doc('master');child=Doc('child')
            composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            composer._style_id2name={'old-id':'old-name'};composer._style_name2id={'old-name':'old-id'}
            try:result=composer._create_style_id_mapping(child);error=None
            except Exception as exc:result=None;error=(type(exc).__name__,exc.args,exc is marker)
            return result,error,trace,composer._style_id2name,composer._style_name2id
        for fail in (False,True):
            with self.subTest(fail=fail):self.assertEqual(run(docxtpl_native,fail),run(docxtpl,fail))

    def test_mapped_style_uses_mapping_protocol_and_preserves_identity(self):
        def run(engine,found):
            master=Document();trace=[];key=object();name=object();value=object()
            class IdMap:
                def __contains__(self,item):trace.append(('contains',item is key));return found
                def __getitem__(self,item):trace.append(('item',item is key));return name
            class NameMap:
                def get(self,item,default):trace.append(('get',item is name,default is key));return value
            obj=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            obj._style_id2name=IdMap();obj._style_name2id=NameMap()
            result=obj.mapped_style_id(key)
            return trace,result is value,result is key
        for found in (False,True):
            with self.subTest(found=found):self.assertEqual(run(docxtpl_native,found),run(docxtpl,found))

    def test_exact_dict_lookup_preserves_attribute_order_and_mutation(self):
        def run(engine):
            trace=[];name1=object();name2=object();result=object()
            class Key:
                def __hash__(self):trace.append('hash');return 7
            key=Key();first={key:name1};second={key:name2};names={name2:result};trace.clear()
            cls=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            class Custom(cls):
                def __getattribute__(self,attribute):
                    if attribute=='_style_id2name':
                        trace.append('id-map');return first if trace.count('id-map')==1 else second
                    if attribute=='_style_name2id':trace.append('name-map');return names
                    return super().__getattribute__(attribute)
            actual=Custom(Document()).mapped_style_id(key)
            return trace,actual is result
        self.assertEqual(run(docxtpl_native),run(docxtpl))


if __name__=='__main__':unittest.main()
