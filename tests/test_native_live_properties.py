"""The native properties loop preserves lookup, callbacks, and partial commits."""
import builtins
import unittest
from unittest.mock import patch

import docxtpl_native


SOURCE_NAME='<string>'
NAMES=('author','comments','identifier','language','subject','title')


def reference(document,session):
    for name in ('author','comments','identifier','language','subject','title'):
        properties=document.docx.core_properties
        initial=getattr(properties,name)
        rendered=session.render('<string>',initial)
        setattr(properties,name,rendered)


class NativeLivePropertiesTests(unittest.TestCase):
    def observe(self,method,*,fail=None,swap=False,mutate_setter=False):
        events=[];failure=ValueError('original failure')
        def late_set(obj,name,value):
            events.append(('late_setattr',name))
            return builtins.setattr(obj,name,value)
        class Properties:
            def __init__(self,label):
                object.__setattr__(self,'label',label)
                for name in NAMES:object.__setattr__(self,name,label+':'+name)
            def __getattribute__(self,name):
                if name in NAMES:events.append(('get',object.__getattribute__(self,'label'),name))
                return object.__getattribute__(self,name)
            def __setattr__(self,name,value):
                if name in NAMES:
                    events.append(('set',object.__getattribute__(self,'label'),name,value))
                    if fail=='set:'+name:raise failure
                object.__setattr__(self,name,value)
        class Model:
            def __init__(self,label):self.properties=Properties(label)
            @property
            def core_properties(self):events.append(('properties',self.properties.label));return self.properties
        class Document:
            def __init__(self):self.model=Model('first');self.second=Model('second')
            @property
            def docx(self):events.append(('docx',self.model.properties.label));return self.model
        class Session:
            def __getattribute__(self,name):
                if name=='render':
                    events.append('lookup_render')
                    if fail=='lookup_render':raise failure
                return object.__getattribute__(self,name)
            def render(self,name,value):
                events.append(('render',name is SOURCE_NAME,value))
                if fail=='render:'+value.split(':')[-1]:raise failure
                if swap and value.endswith(':author'):document.model=document.second
                if mutate_setter and value.endswith(':author'):globals()['setattr']=late_set
                return value.upper()
        document=Document();first=document.model;second=document.second;session=Session()
        try:
            result=method(document,session)
            outcome=('ok',result)
        except Exception as error:
            outcome=('error',type(error),str(error),error is failure)
        state={model.properties.label:dict(object.__getattribute__(model.properties,'__dict__')) for model in (first,second)}
        return outcome,events,state

    def native(self,document,session):
        return docxtpl_native._extension().render_live_properties(document,session,SOURCE_NAME)

    def test_order_and_state(self):
        for kwargs in ({},{'swap':True},{'fail':'set:comments'},{'fail':'render:identifier'},{'fail':'lookup_render'}):
            with self.subTest(**kwargs):self.assertEqual(self.observe(self.native,**kwargs),self.observe(reference,**kwargs))

    def test_dynamic_getattr_and_setattr_globals(self):
        events=[]
        def custom_get(obj,name):events.append(('getattr',name));return builtins.getattr(obj,name)
        def custom_set(obj,name,value):events.append(('setattr',name));return builtins.setattr(obj,name,value)
        with patch.dict(globals(),{'getattr':custom_get,'setattr':custom_set}):
            expected=self.observe(reference);reference_events=events.copy();events.clear()
            actual=self.observe(self.native);native_events=events.copy()
        self.assertEqual(actual,expected)
        self.assertEqual(native_events,reference_events)

    def test_global_setter_changed_during_render_is_used_immediately(self):
        with patch.dict(globals(),{},clear=False):expected=self.observe(reference,mutate_setter=True)
        with patch.dict(globals(),{},clear=False):actual=self.observe(self.native,mutate_setter=True)
        self.assertEqual(actual,expected)


if __name__=='__main__':unittest.main()
