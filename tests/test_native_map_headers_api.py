"""Replacing exposed header parts keeps graph objects and relationships."""
import io
import unittest
from docx import Document
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml.ns import nsmap
import docxtpl
import docxtpl_native


class NativeMapHeadersTests(unittest.TestCase):
    def test_real_part_replacement_keeps_relationship_targets(self):
        def observe(engine):
            document = Document(); header = document.sections[0].header
            header.paragraphs[0].text = 'old'
            old = header.part
            target = Part(PackURI('/word/custom.bin'), 'application/octet-stream', b'data', old.package)
            old.relate_to(target, 'urn:test:internal')
            old.relate_to('https://example.test', 'urn:test:external', is_external=True)
            tpl = engine.DocxTemplate(None); tpl.docx = document
            key = next(k for k, value in document._part.rels.items() if value.target_part is old)
            xml = f'<w:hdr xmlns:w="{nsmap["w"]}"><w:p><w:r><w:t>new</w:t></w:r></w:p></w:hdr>'.encode()
            result = tpl.map_headers_footers_xml(key, xml)
            new = document._part.rels[key].target_part
            rels = [(rel.rId, rel.reltype, rel.is_external,
                     rel._target is target if not rel.is_external else rel._target)
                    for rel in new.rels.values()]
            return result, new is old, new.partname == old.partname, new.content_type == old.content_type, new.package is old.package, rels, new.blob
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_invalid_xml_leaves_original_target_and_error(self):
        def observe(engine):
            document = Document(); old = document.sections[0].header.part
            key = next(k for k, value in document._part.rels.items() if value.target_part is old)
            tpl = engine.DocxTemplate(None); tpl.docx = document
            try: tpl.map_headers_footers_xml(key, b'<invalid>')
            except Exception as error:
                return type(error).__name__, str(error), document._part.rels[key].target_part is old
            raise AssertionError('expected invalid XML')
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_relationship_failure_does_not_replace_old_part(self):
        def observe(engine):
            failure = RuntimeError('relationship failed'); trace = []
            class Rels:
                def items(self):
                    trace.append('items')
                    def values():
                        yield 'a', Relation()
                        raise failure
                    return values()
            class Relation:
                reltype = 'urn:test'; _target = 'https://example.test'; rId = 'rId1'; is_external = True
            class Old:
                partname = PackURI('/word/header1.xml'); content_type = 'application/xml'; package = object(); rels = Rels()
            class Link: target_part = Old()
            class Owner: rels = {'rId1': Link()}
            class Doc: _part = Owner()
            tpl = engine.DocxTemplate(None); tpl.docx = Doc(); old = tpl.docx._part.rels['rId1'].target_part
            try: tpl.map_headers_footers_xml('rId1', b'<root/>')
            except RuntimeError as error: return error is failure, trace, tpl.docx._part.rels['rId1'].target_part is old
            raise AssertionError('expected original exception')
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))
