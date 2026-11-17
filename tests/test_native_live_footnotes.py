"""Live footnote traversal preserves Python protocols and partial state."""
import builtins
from contextlib import nullcontext
import unittest
from unittest.mock import patch

import docxtpl_native


NOTE='application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml'
ENCODING='utf-8'


def reference(document,render_xml):
    for section in document.docx.sections:
        for part in section.part.package.parts:
            if part.content_type=='application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml':
                source=part.blob.decode('utf-8') if isinstance(part.blob,bytes) else part.blob
                part._blob=render_xml(source,part).encode('utf-8')


class NativeLiveFootnotesTests(unittest.TestCase):
    def observe(self,method,*,invalid=False,fail=False,text_blob=False,custom_blob=False,mutate_instance=False):
        events=[];failure=RuntimeError('original failure')
        class Content(str):
            def __eq__(self,other):events.append(('compare',str(self),str(other)));return super().__eq__(other)
        class Part:
            def __init__(self,label,content,blob):
                object.__setattr__(self,'label',label);object.__setattr__(self,'_content',Content(content))
                object.__setattr__(self,'_blob',blob)
            @property
            def content_type(self):events.append(('content_type',self.label));return self._content
            @property
            def blob(self):events.append(('blob',self.label));return self._blob
            def __setattr__(self,name,value):
                if name=='_blob':events.append(('commit',self.label,value))
                object.__setattr__(self,name,value)
        class Package:
            @property
            def parts(self):events.append('parts');return iter(parts)
        class Container:
            package=Package()
        class Section:
            @property
            def part(self):events.append('section_part');return Container()
        class Model:
            @property
            def sections(self):events.append('sections');return iter((Section(),Section()))
        class Document:
            @property
            def docx(self):events.append('docx');return Model()
        class Rendered(str):
            def encode(self,encoding):events.append(('encode',encoding));return super().encode(encoding)
        class Blob(bytes):
            def decode(self,encoding):events.append(('decode',encoding));return super().decode(encoding)
        first_blob='hello' if text_blob else Blob(b'hello') if custom_blob else b'hello'
        parts=[Part('skip','other',b'unchanged'),Part('first',NOTE,first_blob),
               Part('second',NOTE,b'\xff' if invalid else b'world')]
        def late_instance(value,kind):
            events.append('late_isinstance')
            return builtins.isinstance(value,kind)
        def render(source,part):
            events.append(('render',part.label,source))
            if fail and part.label=='second':raise failure
            if mutate_instance and part.label=='first':globals()['isinstance']=late_instance
            return Rendered(source+'!')
        def failing_instance(value,kind):events.append('isinstance_failure');raise failure
        context=patch.dict(globals(),{'isinstance':failing_instance}) if fail=='isinstance' else nullcontext()
        with context:
            try:
                result=method(Document(),render)
                outcome=('ok',result)
            except Exception as error:
                outcome=('error',type(error),str(error),error is failure)
        return outcome,events,[p._blob for p in parts]

    def native(self,document,render):
        return docxtpl_native._extension().render_live_footnotes(document,render,NOTE,ENCODING)

    def test_order_repeated_sections_and_partial_failures(self):
        for options in ({},{'text_blob':True},{'custom_blob':True},{'invalid':True},{'fail':True},{'fail':'isinstance'}):
            with self.subTest(**options):self.assertEqual(self.observe(self.native,**options),self.observe(reference,**options))

    def test_dynamic_isinstance_and_bytes_globals(self):
        events=[]
        def custom_instance(value,kind):events.append(('isinstance',kind));return builtins.isinstance(value,kind)
        with patch.dict(globals(),{'isinstance':custom_instance,'bytes':builtins.bytes}):
            expected=self.observe(reference);reference_events=events.copy();events.clear()
            actual=self.observe(self.native);native_events=events.copy()
        self.assertEqual(actual,expected)
        self.assertEqual(native_events,reference_events)

    def test_type_check_changed_during_render_is_used_for_later_parts(self):
        with patch.dict(globals(),{},clear=False):expected=self.observe(reference,mutate_instance=True)
        with patch.dict(globals(),{},clear=False):actual=self.observe(self.native,mutate_instance=True)
        self.assertEqual(actual,expected)


if __name__=='__main__':unittest.main()
