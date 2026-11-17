"""Picture pre-processing is driven in Rust over the exposed document graph."""
import io
import unittest
from PIL import Image
from docx import Document
import docxtpl
import docxtpl_native

def png(color):
    buf = io.BytesIO(); Image.new('RGB', (2, 2), color).save(buf, format='PNG'); return buf.getvalue()

def real_document(engine, key, allow_missing=False):
    doc = Document(); doc.add_paragraph().add_run().add_picture(io.BytesIO(png('red')))
    header = doc.sections[0].header
    header.paragraphs[0].add_run().add_picture(io.BytesIO(png('green')))
    tpl = engine.DocxTemplate(None); tpl.docx = doc; tpl.pic_map = {}
    tpl.pics_to_replace = {key: png('blue')}; tpl.allow_missing_pics = allow_missing
    try:
        value = tpl.pre_processing(); error = None
    except Exception as exc:
        value = None; error = (type(exc).__name__, str(exc))
    mapping = sorted((name, ref, part.blob) for name, (ref, part) in tpl.pic_map.items())
    def images(part):
        return sorted(rel.target_part.blob for rel in part.rels.values() if rel.reltype.endswith('/image'))
    return value, error, mapping, images(doc.part), images(header.part)

class NativePreprocessingTests(unittest.TestCase):
    def test_real_pictures_main_and_header(self):
        for key, allow in [('image.png', False), ('missing', False), ('missing', True)]:
            with self.subTest(key=key, allow=allow):
                actual = real_document(docxtpl_native, key, allow)
                self.assertEqual(actual, real_document(docxtpl, key, allow))
                if key == 'image.png':
                    self.assertIsNone(actual[1])
                    self.assertEqual(actual[3], [png('blue')])
                    self.assertEqual(actual[4], [png('blue')])
                else:
                    self.assertEqual(actual[3], [png('red')])
                    self.assertEqual(actual[4], [png('green')])
                    self.assertEqual(actual[1] is None, allow)

    def test_noop_and_dynamic_override(self):
        def observe(engine):
            calls = []
            class Template(engine.DocxTemplate):
                def _replace_pics(self): calls.append('replace')
            tpl = Template(None); tpl.pre_processing(); tpl.pics_to_replace = {'x': b'x'}
            result = tpl.pre_processing()
            return result, calls
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_title_description_and_invalid_graphic(self):
        def observe(engine, key):
            doc = Document(); doc.add_paragraph().add_run().add_picture(io.BytesIO(png('red')))
            doc.add_paragraph().add_run().add_picture(io.BytesIO(png('green')))
            graphics = doc._element.xpath('//a:graphicData')
            del graphics[0].attrib['uri']
            properties = graphics[1].xpath('pic:pic/pic:nvPicPr/pic:cNvPr')[0]
            properties.set('title', 'my-title'); properties.set('descr', 'my-description')
            tpl = engine.DocxTemplate(None); tpl.docx = doc; tpl.pic_map = {}
            tpl.pics_to_replace = {key: png('blue')}; tpl.pre_processing()
            return sorted((name, ref, part.blob) for name, (ref, part) in tpl.pic_map.items())
        for key in ('my-title', 'my-description'):
            with self.subTest(key=key):
                self.assertEqual(observe(docxtpl_native, key), observe(docxtpl, key))

    def test_dynamic_parts_and_partial_failure(self):
        def observe(engine):
            calls = []
            class Rel:
                reltype = engine.DocxTemplate.HEADER_URI
                target_part = 'header'
            class Part: rels = {'r1': Rel()}
            class Doc: part = Part()
            class Template(engine.DocxTemplate):
                def _replace_docx_part_pics(self, part, replaced):
                    calls.append(('header' if part == 'header' else 'body', list(replaced.items())))
                    if part != 'header': replaced['first'] = True
            tpl = Template(None); tpl.docx = Doc(); tpl.pics_to_replace = {'first': b'a', 'missing': b'b'}
            try: tpl.pre_processing()
            except Exception as exc: return calls, type(exc).__name__, str(exc)
            raise AssertionError('expected missing image')
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_picture_key_exceptions_follow_reference(self):
        def observe(engine, exception_type):
            doc = Document(); doc.add_paragraph().add_run().add_picture(io.BytesIO(png('red')))
            class Key:
                def __hash__(self): return 17
                def __eq__(self, other): raise exception_type('comparison failed')
            tpl = engine.DocxTemplate(None); tpl.docx = doc; tpl.pic_map = {}; tpl.allow_missing_pics = True
            tpl.pics_to_replace = {Key(): png('blue')}
            try:
                result = tpl.pre_processing()
                error = None
            except BaseException as exc:
                result = None; error = (type(exc).__name__, str(exc))
            return result, error, sorted(tpl.pic_map)
        for exception_type in (RuntimeError, KeyboardInterrupt):
            with self.subTest(exception_type=exception_type):
                self.assertEqual(observe(docxtpl_native, exception_type), observe(docxtpl, exception_type))

if __name__ == '__main__': unittest.main()
