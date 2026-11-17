"""Differential checks for the programmatic subdocument binding."""
import importlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import docxtpl
import docxtpl_native
from docx import Document
from PIL import Image


def template_bytes():
    doc = Document()
    doc.add_paragraph('{{p child }}')
    data = io.BytesIO()
    doc.save(data)
    return data.getvalue()


def build(engine):
    template = engine.DocxTemplate(io.BytesIO(template_bytes()))
    template.render_init()
    child = template.new_subdoc()
    child.add_paragraph('Hello ✓')
    image = io.BytesIO()
    Image.new('RGB', (4, 3), (23, 42, 81)).save(image, 'PNG')
    image.seek(0)
    child.add_picture(image)
    shared_part = child.subdocx._part is template.get_docx()._part
    xml = str(child)
    html = child.__html__()
    template.render({'child': child})
    result = io.BytesIO()
    template.save(result)
    document = Document(io.BytesIO(result.getvalue()))
    return shared_part, xml, html, [p.text for p in document.paragraphs], len(document.inline_shapes)


class NativeProgrammaticSubdocTests(unittest.TestCase):
    def test_builder_graph_xml_and_complete_document(self):
        reference = build(docxtpl)
        self.assertTrue(reference[0])
        self.assertEqual(reference[3:], (['Hello ✓', ''], 1))
        self.assertEqual(build(docxtpl_native), reference)

    def test_attribute_forwarding_and_repeat_xml(self):
        for engine in (docxtpl, docxtpl_native):
            with self.subTest(engine=engine.__name__):
                template = engine.DocxTemplate(io.BytesIO(template_bytes()))
                child = template.new_subdoc()
                self.assertEqual(child.__getattr__('add_paragraph').__self__, child.subdocx)
                child.add_paragraph('one')
                self.assertIsNotNone(child.subdocx.element.body.sectPr)
                first = str(child)
                self.assertIsNone(child.subdocx.element.body.sectPr)
                self.assertEqual(str(child), first)
                self.assertEqual(child.__unicode__(), first)
                self.assertEqual(child.__html__(), first)
                with self.assertRaises(AttributeError):
                    getattr(child, 'nonexistent_subdoc_member')

    def test_reference_builder_is_not_called(self):
        import docxtpl.subdoc as reference
        template = docxtpl_native.DocxTemplate(io.BytesIO(template_bytes()))
        with patch.object(reference, 'Subdoc', side_effect=AssertionError('reference builder called')):
            child = template.new_subdoc()
            child.add_paragraph('native')
            self.assertIn('native', str(child))

    def test_programmatic_subdoc_has_public_type_and_live_routing(self):
        from docxtpl_native.bridge import collect_specials, requires_live_model
        template = docxtpl_native.DocxTemplate(io.BytesIO(template_bytes()))
        child = template.new_subdoc()
        self.assertIs(type(child), docxtpl_native.Subdoc)
        self.assertIsInstance(child, docxtpl_native.Subdoc)
        self.assertTrue(requires_live_model({'child': child}, None))
        self.assertEqual(collect_specials({'child': child}, SimpleNamespace(globals={})), [])
        child.add_paragraph('live')
        template.render({'child': child})
        output = io.BytesIO()
        template.save(output)
        self.assertEqual(Document(io.BytesIO(output.getvalue())).paragraphs[0].text, 'live')

    def test_file_subdoc_preserves_special_collection_and_string_error(self):
        from docxtpl_native.bridge import collect_specials, requires_live_model
        child = object.__new__(docxtpl_native.Subdoc)
        child.tpl = None
        child.source = b'file source'
        self.assertIs(type(child), docxtpl_native.Subdoc)
        self.assertFalse(requires_live_model({'child': child}, None))
        specials = collect_specials({'child': child}, SimpleNamespace(globals={}))
        self.assertEqual([entry['identity'] for entry in specials], [id(child)])
        self.assertEqual(specials[0]['value']['kind'], 'subdoc')
        with self.assertRaises(docxtpl_native.UnsupportedFeature):
            str(child)

    def test_invalid_template_constructor_matches_reference(self):
        def run(engine, source):
            try:
                engine.Subdoc(None, source)
            except Exception as error:
                return type(error), str(error)
            self.fail('expected invalid template failure')
        for source in (None, b'file source'):
            with self.subTest(source=source):
                self.assertEqual(run(docxtpl_native, source), run(docxtpl, source))

    def test_file_subdoc_live_graph_and_serializer(self):
        file_doc = Document()
        file_doc.add_paragraph('file child')
        data = io.BytesIO()
        file_doc.save(data)
        def run(engine):
            template = engine.DocxTemplate(io.BytesIO(template_bytes()))
            child = template.new_subdoc(io.BytesIO(data.getvalue()))
            xml = str(child)
            return xml, child.docx is template.get_docx(), child.__html__()
        self.assertEqual(run(docxtpl_native), run(docxtpl))

    def test_file_subdoc_composition_failure_keeps_reference_partial_state(self):
        def run(engine):
            module = importlib.import_module(engine.__name__ + '.subdoc')
            template = engine.DocxTemplate(io.BytesIO(template_bytes()))
            error = RuntimeError('attach failed')
            calls, captured = [], []
            class Captured(engine.Subdoc):
                def __new__(cls, *args, **kwargs):
                    instance = object.__new__(cls)
                    captured.append(instance)
                    return instance
            class Composer:
                def __init__(self, master):
                    calls.append('init')
                def attach_parts(self, document):
                    calls.append('attach')
                    raise error
            with patch.object(module, 'SubdocComposer', Composer):
                with self.assertRaises(RuntimeError) as caught:
                    Captured(template, io.BytesIO(template_bytes()))
            instance = captured[0]
            return (caught.exception is error, calls, instance.tpl is template,
                    instance.docx is template.get_docx(), 'subdocx' in instance.__dict__)
        self.assertEqual(run(docxtpl_native), run(docxtpl))

    def test_file_subdoc_load_failure_keeps_reference_partial_state(self):
        def run(engine):
            module = importlib.import_module(engine.__name__ + '.subdoc')
            template = engine.DocxTemplate(io.BytesIO(template_bytes()))
            error, captured = RuntimeError('load failed'), []
            class Captured(engine.Subdoc):
                def __new__(cls, *args, **kwargs):
                    instance = object.__new__(cls)
                    captured.append(instance)
                    return instance
            def load(source):
                raise error
            with patch.object(module, 'Document', load):
                with self.assertRaises(RuntimeError) as caught:
                    Captured(template, io.BytesIO(template_bytes()))
            instance = captured[0]
            return (caught.exception is error, instance.tpl is template,
                    instance.docx is template.get_docx(), 'subdocx' in instance.__dict__)
        self.assertEqual(run(docxtpl_native), run(docxtpl))

    def test_file_source_truthiness_and_callbacks_match_reference(self):
        def run(engine, truthy):
            module = importlib.import_module(engine.__name__ + '.subdoc')
            trace = []
            master, child_doc = Document(), Document()
            child_doc.add_paragraph('child')
            class Template:
                def get_docx(self):
                    trace.append('get_docx')
                    return master
            class Source:
                def __bool__(self):
                    trace.append('source_bool')
                    return truthy
            source = Source()
            def load(value):
                trace.append(('document', value is source))
                return child_doc
            class Composer:
                def __init__(self, value):
                    trace.append(('composer', value is master))
                def attach_parts(self, value):
                    trace.append(('attach', value is child_doc))
            with patch.object(module, 'Document', load), patch.object(module, 'SubdocComposer', Composer):
                child = engine.Subdoc(Template(), source)
                xml = str(child)
            return trace, xml, child.docx is master, child.subdocx is child_doc, child_doc._part is master._part
        for truthy in (False, True):
            with self.subTest(truthy=truthy):
                self.assertEqual(run(docxtpl_native, truthy), run(docxtpl, truthy))

    def test_construction_preserves_callback_order_and_identity(self):
        def run(engine):
            module = importlib.import_module(engine.__name__ + '.subdoc')
            trace = []
            part = object()
            master = SimpleNamespace(_part=part)
            class Template:
                def get_docx(self):
                    trace.append('get_docx')
                    return master
            child_doc = SimpleNamespace(_part=None)
            def document(path):
                trace.append(('document', path))
                return child_doc
            with patch.object(module, 'Document', document):
                child = engine.Subdoc(Template())
            return trace, child.docx is master, child.subdocx is child_doc, child_doc._part is part
        self.assertEqual(run(docxtpl_native), run(docxtpl))
        self.assertEqual(run(docxtpl_native), (['get_docx', ('document', None)], True, True, True))

    def test_unicode_strip_and_serializer_callback(self):
        def run(engine, text):
            module = importlib.import_module(engine.__name__ + '.subdoc')
            template = engine.DocxTemplate(io.BytesIO(template_bytes()))
            child = engine.Subdoc(template)
            events = []
            def tostring(body, **kwargs):
                events.append((body is child.subdocx.element.body, kwargs))
                return text
            with patch.object(module, 'etree', SimpleNamespace(tostring=tostring)):
                result = child._get_xml()
            return result, events
        for text in ('<w:body x="1">A\ud800B</w:body>', 'x<w:bodymore\n a>inner</w:body>y', 'before<w:body'):
            with self.subTest(text=ascii(text)):
                self.assertEqual(run(docxtpl_native, text), run(docxtpl, text))

    def test_body_tag_substitution_matches_reference_for_varied_unicode(self):
        import random
        randomizer = random.Random(4217)
        fragments = ('x', '\n', '✓', '👶', '\ud800', '<', '>', '<w:body',
                     '</w:body', '/>', ' attribute="1"', '<w:p/>')
        texts = ['café', '中文', '👶', '<w:body>façade</w:body>', '<w:body>中文</w:body>']
        texts += [''.join(randomizer.choice(fragments) for _ in range(20)) for _ in range(100)]
        results = []
        for engine in (docxtpl, docxtpl_native):
            module = importlib.import_module(engine.__name__ + '.subdoc')
            child = engine.Subdoc(engine.DocxTemplate(io.BytesIO(template_bytes())))
            output = []
            for text in texts:
                with patch.object(module, 'etree', SimpleNamespace(tostring=lambda *args, **kwargs: text)):
                    output.append(child._get_xml())
            results.append(output)
        self.assertEqual(results[1], results[0])

    def test_serializer_string_identity_matches_reference(self):
        class StringSubclass(str):
            pass
        def run(engine, value):
            module = importlib.import_module(engine.__name__ + '.subdoc')
            child = engine.Subdoc(engine.DocxTemplate(io.BytesIO(template_bytes())))
            with patch.object(module, 'etree', SimpleNamespace(tostring=lambda *args, **kwargs: value)):
                result = child._get_xml()
            return type(result), result is value, result
        for value in ('plain text', '', StringSubclass('plain text'), '<w:body>x</w:body>'):
            with self.subTest(value=repr(value), kind=type(value).__name__):
                self.assertEqual(run(docxtpl_native, value), run(docxtpl, value))

    def test_serializer_exception_identity(self):
        for engine in (docxtpl, docxtpl_native):
            module = importlib.import_module(engine.__name__ + '.subdoc')
            child = engine.Subdoc(engine.DocxTemplate(io.BytesIO(template_bytes())))
            error = RuntimeError('serialize failed')
            def tostring(*args, **kwargs):
                raise error
            with patch.object(module, 'etree', SimpleNamespace(tostring=tostring)):
                with self.assertRaises(RuntimeError) as caught:
                    str(child)
            self.assertIs(caught.exception, error)
            self.assertIsNone(child.subdocx.element.body.sectPr)

    def test_invalid_serializer_result_matches_reference_error(self):
        def run(engine, value):
            module = importlib.import_module(engine.__name__ + '.subdoc')
            child = engine.Subdoc(engine.DocxTemplate(io.BytesIO(template_bytes())))
            with patch.object(module, 'etree', SimpleNamespace(tostring=lambda *args, **kwargs: value)):
                try:
                    child._get_xml()
                except Exception as error:
                    return type(error), str(error)
            self.fail('expected a serializer result error')
        for value in (None, 5, b'xml', bytearray(b'xml'), memoryview(b'xml'), memoryview(b'123456')[::2]):
            with self.subTest(value=type(value).__name__):
                self.assertEqual(run(docxtpl_native, value), run(docxtpl, value))

    def test_dynamic_body_lookups_and_removal_order(self):
        def run(engine):
            module = importlib.import_module(engine.__name__ + '.subdoc')
            events = []
            section = object()
            class Body:
                @property
                def sectPr(self):
                    events.append('section')
                    return section
                def remove(self, value):
                    events.append(('remove', value is section))
            body = Body()
            class Element:
                @property
                def body(self):
                    events.append('body')
                    return body
            child_doc = SimpleNamespace(_part=None, element=Element())
            template = SimpleNamespace(get_docx=lambda: SimpleNamespace(_part=object()))
            def serialize(element, **kwargs):
                events.append(('serialize', element is body))
                return '<w:body><w:p/></w:body>'
            with patch.object(module, 'Document', lambda path: child_doc), \
                 patch.object(module, 'etree', SimpleNamespace(tostring=serialize)):
                child = engine.Subdoc(template)
                result = child._get_xml()
            return result, events
        self.assertEqual(run(docxtpl_native), run(docxtpl))

    def test_subclass_attribute_hook_observes_reference_order(self):
        def run(engine):
            events = []
            class Observed(engine.Subdoc):
                def __getattribute__(self, name):
                    events.append(name)
                    return super().__getattribute__(name)
            template = engine.DocxTemplate(io.BytesIO(template_bytes()))
            child = Observed(template)
            child.add_paragraph('text')
            events.clear()
            result = str(child)
            return result, events
        self.assertEqual(run(docxtpl_native), run(docxtpl))


if __name__ == '__main__':
    unittest.main()
