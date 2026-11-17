"""Direct footnote rendering preserves exposed object protocols."""
import io
import sys
import unittest
from docx import Document
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml.ns import nsmap
import jinja2
import docxtpl
import docxtpl_native

NOTE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes'


class NativeRenderFootnotesTests(unittest.TestCase):
    def test_real_footnote_mutates_same_part(self):
        def observe(engine):
            doc = Document(); doc.add_paragraph('body')
            xml = f'<w:footnotes xmlns:w="{nsmap["w"]}"><w:footnote w:id="1"><w:p><w:r><w:t>{{{{ value }}}}</w:t></w:r></w:p></w:footnote></w:footnotes>'.encode()
            note = Part(PackURI('/word/footnotes.xml'), NOTE, xml, doc.part.package)
            doc.part.relate_to(note, REL)
            tpl = engine.DocxTemplate(None); tpl.docx = doc
            result = tpl.render_footnotes({'value':'X'})
            return result, note is doc.part.rels.part_with_reltype(REL), note.blob
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_access_order_and_original_environment(self):
        def observe(engine):
            trace = []; env = object(); context = object()
            class Blob(bytes):
                def decode(self, encoding): trace.append(('decode', encoding)); return 'xml'
            class Rendered(str):
                def encode(self, encoding): trace.append(('encode', encoding)); return b'rendered'
            class Note:
                content_type = NOTE
                @property
                def blob(self): trace.append('blob'); return Blob(b'xml')
                def __setattr__(self, name, value): trace.append(('set', name, value)); object.__setattr__(self, name, value)
            note = Note()
            class Package:
                @property
                def parts(self): trace.append('parts'); return [note]
            class Part:
                @property
                def package(self): trace.append('package'); return Package()
            class Section:
                @property
                def part(self): trace.append('part'); return Part()
            class Doc:
                @property
                def sections(self): trace.append('sections'); return [Section(), Section()]
            class Template(engine.DocxTemplate):
                def patch_xml(self, xml): trace.append(('patch', xml)); return 'patched'
                def render_xml_part(self, xml, part, actual_context, actual_env):
                    trace.append(('render', xml, part is note, actual_context is context, actual_env is env)); return Rendered('x')
            tpl = Template(None); tpl.docx = Doc()
            result = tpl.render_footnotes(context, env)
            return result, trace
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_literal_identity_and_spoofed_bytes_protocol(self):
        def observe(engine):
            types = []; encodings = []; trace = []
            note_interned = sys.intern(NOTE); utf8_interned = sys.intern('utf-8')
            class Kind:
                def __eq__(self, value): types.append(value); return True
            class Blob:
                @property
                def __class__(self): trace.append('class'); return bytes
                def decode(self, encoding): encodings.append(encoding); return 'xml'
            class Rendered(str):
                def encode(self, encoding): encodings.append(encoding); return b'new'
            class Note: content_type = Kind(); blob = Blob()
            class Package: parts = [Note()]
            class Part: package = Package()
            class Section: part = Part()
            class Doc: sections = [Section(), Section()]
            class Template(engine.DocxTemplate):
                def patch_xml(self, xml): return xml
                def render_xml_part(self, xml, part, context, env): return Rendered('x')
            tpl = Template(None); tpl.docx = Doc(); tpl.render_footnotes({}, object())
            return (len(types), types[0] is types[1], types[0] is note_interned,
                    len(encodings), all(x is encodings[0] for x in encodings),
                    encodings[0] is utf8_interned, trace)
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_default_environment_is_shared_and_partial_errors_preserve_state(self):
        def observe(engine):
            failure = RuntimeError('failure'); seen = []
            class Note:
                content_type = NOTE
                def __init__(self, index): self.index = index; self._blob = b'old'
                @property
                def blob(self): return self._blob
            notes = [Note(0), Note(1)]
            class Package: parts = notes
            class Part: package = Package()
            class Section: part = Part()
            class Doc: sections = [Section()]
            class Template(engine.DocxTemplate):
                def patch_xml(self, xml): return xml
                def render_xml_part(self, xml, part, context, env):
                    seen.append((part.index, isinstance(env, jinja2.Environment), id(env)))
                    if part.index == 1: raise failure
                    return 'new'
            tpl = Template(None); tpl.docx = Doc()
            try: tpl.render_footnotes({})
            except RuntimeError as error:
                return error is failure, [x._blob for x in notes], [(a,b) for a,b,_ in seen], seen[0][2] == seen[1][2]
            raise AssertionError('expected callback error')
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))
