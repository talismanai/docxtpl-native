"""Inline image callbacks and mutation order, compared with docxtpl."""
import io
import builtins
import subprocess
import sys
import unittest
from unittest.mock import patch

import docxtpl
import docxtpl.inline_image
import docxtpl_native
import docxtpl_native.inline_image


class InlineImageProtocolTests(unittest.TestCase):
    def test_attribute_and_method_lookup_order(self):
        def observe(cls, properties):
            trace=[]
            class Image(cls):
                def __getattribute__(self,name):
                    if not name.startswith('__'):trace.append('image.'+name)
                    return super().__getattribute__(name)
            class Picture:
                def xpath(self,expression):
                    trace.append('xpath:'+expression)
                    if expression=='.//a:blip':return [object()] if properties else []
                    return [Node()]
                @property
                def xml(self):trace.append('picture.xml');return '<picture/>'
            class Node:
                def set(self,name,value):trace.append('set:'+name+':'+value)
                def append(self,value):trace.append('append:'+value.tag)
            class Part:
                def __getattribute__(self,name):
                    if not name.startswith('__'):trace.append('part.'+name)
                    return super().__getattribute__(name)
                def new_pic_inline(self,*args):trace.append('new_picture');return Picture()
                def relate_to(self,*args,**kwargs):trace.append('relate');return 'rId7'
            class Template:
                @property
                def current_rendering_part(self):trace.append('current_part');return Part()
            image=Image(Template(),'source',anchor='url',title='title' if properties else None,descr='descr' if properties else None)
            trace.clear()
            result=image._insert_image()
            return result,trace
        for properties in (False,True):
            with self.subTest(properties=properties):
                self.assertEqual(observe(docxtpl_native.InlineImage,properties),observe(docxtpl.InlineImage,properties))

    def observe(self, image_class, *, title=None, descr=None, anchor=None, blip=True):
        trace=[]

        class Node:
            def __init__(self, name): self.name=name
            def set(self, name, value): trace.append(('set',self.name,name,value))
            def append(self, value): trace.append(('append',self.name,value.tag,value.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')))

        class Picture:
            def xpath(self, expression):
                trace.append(('xpath',expression))
                if expression=='.//a:blip': return [object()] if blip else []
                return [Node(expression)]
            @property
            def xml(self):
                trace.append(('xml',))
                return '<picture/>'

        class Part:
            def new_pic_inline(self, source, width, height):
                trace.append(('new_picture',source,width,height))
                return Picture()
            def relate_to(self, url, relationship, is_external=False):
                trace.append(('relate',url,relationship,is_external))
                return 'rId7'

        class Template:
            current_rendering_part=Part()

        image=image_class(Template(),'source',width=12,height=34,anchor=anchor,title=title,descr=descr)
        return image._insert_image(), trace

    def test_picture_properties_hyperlink_and_order(self):
        expected, trace=self.observe(docxtpl.InlineImage,title=42,descr=False,anchor='https://example.test')
        actual, native_trace=self.observe(docxtpl_native.InlineImage,title=42,descr=False,anchor='https://example.test')
        self.assertEqual((actual,native_trace),(expected,trace))
        self.assertEqual(actual,'</w:t></w:r><w:r><w:drawing><picture/></w:drawing></w:r><w:r><w:t xml:space="preserve">')
        self.assertEqual([x[0] for x in trace],['new_picture','xpath','xpath','set','set','set','set','xpath','relate','xpath','xpath','append','append','xml'])
        self.assertEqual([x[3] for x in trace if x[0]=='set'],['42','False','42','False'])

    def test_absent_blip_skips_hyperlink(self):
        for cls in (docxtpl.InlineImage,docxtpl_native.InlineImage):
            with self.subTest(cls=cls):
                _,trace=self.observe(cls,anchor='url',blip=False)
                self.assertEqual([x[0] for x in trace],['new_picture','xpath','xml'])

    def test_hyperlink_does_not_invoke_import_hook(self):
        original=builtins.__import__
        docxtpl_native._extension()
        for cls in (docxtpl.InlineImage,docxtpl_native.InlineImage):
            imports=[]
            def tracked(name,*args,**kwargs):
                imports.append(name)
                return original(name,*args,**kwargs)
            with self.subTest(cls=cls),patch.object(builtins,'__import__',tracked):
                self.observe(cls,anchor='url')
            self.assertEqual(imports,[])

    # Each class reads the globals of its own module: docxtpl.inline_image for
    # the reference and docxtpl_native.inline_image for this package.
    def test_hyperlink_observes_patched_xml_globals(self):
        def observe(cls,module):
            original_element,original_qn=module.OxmlElement,module.qn
            trace=[]
            def element(name):trace.append(('element',name));return original_element(name)
            def qn(name):trace.append(('qn',name));return original_qn(name)
            with patch.object(module,'OxmlElement',element),patch.object(module,'qn',qn):
                result,events=self.observe(cls,anchor='url')
            return result,events,trace
        self.assertEqual(observe(docxtpl_native.InlineImage,docxtpl_native.inline_image),
                         observe(docxtpl.InlineImage,docxtpl.inline_image))

    def test_constructor_observes_patched_str_global(self):
        def observe(cls,module):
            trace=[]
            def stringify(value):trace.append(value);return 'wrapped:'+value
            with patch.object(module,'str',stringify,create=True):
                image=cls(None,'source',title='title',descr='description')
            return image.title,image.descr,trace
        self.assertEqual(observe(docxtpl_native.InlineImage,docxtpl_native.inline_image),
                         observe(docxtpl.InlineImage,docxtpl.inline_image))

    def test_image_module_removed_before_native_load(self):
        code=("import sys,docxtpl_native; sys.modules.pop('docxtpl_native.inline_image',None); "
              "assert docxtpl_native.InlineImage(None,'source',title='title').title == 'title'")
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_insert_after_bypassing_constructor_loads_extension(self):
        code=("import docxtpl_native as d; "
              "P=type('P',(),{'xml':'x'}); "
              "Part=type('Part',(),{'new_pic_inline':lambda *a:P()}); "
              "T=type('T',(),{'current_rendering_part':Part()}); "
              "image=object.__new__(d.InlineImage); "
              "image.tpl=T(); image.image_descriptor='source'; "
              "assert 'x' in image._insert_image()")
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_xml_formatting_preserves_unicode_and_custom_values(self):
        class CustomString(str):
            def __str__(self): return 'custom'
        class CustomObject:
            def __str__(self): return 'object'
        class Picture:
            def __init__(self,xml):self.xml=xml
        class Part:
            def __init__(self,xml):self.xml=xml
            def new_pic_inline(self,*args):return Picture(self.xml)
        class Template:
            def __init__(self,xml):self.current_rendering_part=Part(xml)
        for xml in ('','text','Café 🐍','a\ud800b','x'*10000,CustomString('inner'),CustomObject()):
            with self.subTest(type=type(xml),length=len(xml) if isinstance(xml,str) else None):
                reference=docxtpl.InlineImage(Template(xml),'source')._insert_image()
                native=docxtpl_native.InlineImage(Template(xml),'source')._insert_image()
                self.assertEqual(native,reference)
        def outcome(cls,xml):
            try:return ('ok',cls(Template(xml),'source')._insert_image())
            except Exception as error:return (type(error),str(error))
        for xml in (('one',),('one','two')):
            with self.subTest(xml=xml):
                self.assertEqual(outcome(docxtpl_native.InlineImage,xml),outcome(docxtpl.InlineImage,xml))

    def test_override_hooks_and_original_error_identity(self):
        for cls in (docxtpl.InlineImage,docxtpl_native.InlineImage):
            with self.subTest(cls=cls):
                trace=[];failure=RuntimeError('property failure')
                class Image(cls):
                    def _set_doc_properties(self, pic):
                        trace.append('properties')
                        raise failure
                    def _add_hyperlink(self, *args):
                        trace.append('hyperlink')
                        return args[0]
                class Part:
                    def new_pic_inline(self, *args):
                        trace.append('new_picture')
                        return object()
                class Template: current_rendering_part=Part()
                image=Image(Template(),'source',title='title',anchor='url')
                with self.assertRaises(RuntimeError) as caught: image._insert_image()
                self.assertIs(caught.exception,failure)
                self.assertEqual(trace,['new_picture','properties'])


if __name__=='__main__': unittest.main()
