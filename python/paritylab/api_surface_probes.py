"""Direct public API probes; missing candidate methods are real differential gaps.

These normal-input observations do not close error/lifecycle/interaction partitions.
No reference method is attached to the candidate to make a probe pass.
"""
import importlib
import io
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsmap
from docx.opc.part import Part
from docx.opc.packuri import PackURI
from lxml import etree
from PIL import Image
from .protocol_base import Probe, Result, template

W = nsmap['w']
BODY = f'<w:body xmlns:w="{W}"><w:p><w:r><w:t>X</w:t></w:r></w:p></w:body>'


def core(method):
    def execute(engine):
        tpl = engine.DocxTemplate(io.BytesIO(template('{{ value }}', header='{{ value }}')))
        tpl.render_init()
        document = tpl.docx
        part = document.part
        if method == 'init_docx':
            tpl.init_docx(reload=False)
            observation = {'same_document': tpl.docx is document, 'rendered': tpl.is_rendered}
        elif method == 'render_init':
            tpl.pic_map['old'] = 'old'; tpl.docx_ids_index = 7; tpl.is_saved = True
            tpl.render_init()
            observation = {'same_document': tpl.docx is document, 'pic_map': tpl.pic_map,
                           'index': tpl.docx_ids_index, 'saved': tpl.is_saved,
                           'part': tpl.current_rendering_part}
        elif method == '__getattr__':
            observation = {'text': tpl.paragraphs[0].text,
                           'same_part': tpl.__getattr__('part') is part}
        elif method == 'render_xml_part':
            xml = tpl.render_xml_part('<w:p><w:r><w:t>{{ value }}</w:t></w:r></w:p>', part, {'value': 'X'})
            observation = {'xml': xml, 'same_part': tpl.current_rendering_part is part}
        elif method == 'render_properties':
            document.core_properties.title = '{{ value }}'
            tpl.render_properties({'value': 'X'})
            observation = {'title': document.core_properties.title}
        elif method == 'render_footnotes':
            content_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml'
            xml = f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="1"><w:p><w:r><w:t>{{{{ value }}}}</w:t></w:r></w:p></w:footnote></w:footnotes>'
            note = Part(PackURI('/word/footnotes.xml'), content_type, xml.encode(), part.package)
            part.relate_to(note, 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes')
            tpl.render_footnotes({'value': 'X'})
            observation = {'text': ''.join(etree.fromstring(note.blob).itertext())}
        elif method == 'resolve_listing':
            xml = tpl.resolve_listing('<w:p><w:r><w:t>A\tB\nC</w:t></w:r></w:p>')
            observation = {'xml': xml}
        elif method == 'build_xml':
            xml = tpl.build_xml({'value': 'X'})
            observation = {'text': ''.join(etree.fromstring(xml.encode()).itertext())}
        elif method == 'map_tree':
            tree = parse_xml(BODY)
            tpl.map_tree(tree)
            observation = {'same_body': document.element.body is tree, 'text': document.paragraphs[0].text}
        elif method == 'get_headers_footers':
            headers = list(tpl.get_headers_footers(tpl.HEADER_URI))
            observation = {'count': len(headers), 'same_part': headers[0][1] is document.sections[0].header.part}
        elif method == 'get_part_xml':
            xml = tpl.get_part_xml(document.sections[0].header.part)
            observation = {'text': ''.join(etree.fromstring(xml.encode()).itertext()), 'declaration': xml.startswith('<?xml')}
        elif method == 'get_headers_footers_encoding':
            observation = {'encodings': [tpl.get_headers_footers_encoding(value) for value in
                ['<?xml version="1.0" encoding="ISO-8859-1"?><root/>', '<root/>']]}
        elif method == 'build_headers_footers_xml':
            items = list(tpl.build_headers_footers_xml({'value': 'X'}, tpl.HEADER_URI))
            observation = {'count': len(items), 'bytes': isinstance(items[0][1], bytes),
                           'text': ''.join(etree.fromstring(items[0][1]).itertext())}
        elif method == 'map_headers_footers_xml':
            key, old = next(tpl.get_headers_footers(tpl.HEADER_URI))
            xml = f'<w:hdr xmlns:w="{W}"><w:p><w:r><w:t>X</w:t></w:r></w:p></w:hdr>'.encode()
            tpl.map_headers_footers_xml(key, xml)
            new = part.rels[key].target_part
            observation = {'replaced': new is not old, 'same_name': new.partname == old.partname,
                           'text': ''.join(etree.fromstring(new.blob).itertext())}
        elif method == 'fix_tables':
            xml = f'<w:body xmlns:w="{W}"><w:tbl><w:tblGrid><w:gridCol w:w="100"/></w:tblGrid><w:tr><w:tc/><w:tc/></w:tr></w:tbl></w:body>'
            tree = tpl.fix_tables(xml)
            observation = {'widths': tree.xpath('//w:gridCol/@w:w', namespaces={'w': W})}
        elif method == 'fix_docpr_ids':
            tree = etree.fromstring(f'<root xmlns:wp="{nsmap["wp"]}"><wp:docPr id="3"/><wp:docPr id="3"/></root>')
            tpl.fix_docpr_ids(tree)
            observation = {'ids': [node.get('id') for node in tree], 'index': tpl.docx_ids_index}
        elif method == 'post_processing':
            buffer = io.BytesIO(b'unchanged'); buffer.seek(2)
            result = tpl.post_processing(buffer)
            observation = {'result': result, 'bytes': buffer.getvalue().decode(), 'position': buffer.tell()}
        elif method == 'pre_processing':
            observation = {'result': tpl.pre_processing(), 'same_document': tpl.docx is document}
        elif method == 'get_pic_map':
            tpl.pic_map['marker'] = 1
            observation = {'same_map': tpl.get_pic_map() is tpl.pic_map, 'value': tpl.get_pic_map()['marker']}
        else:
            raise AssertionError(method)
        return Result(observation, {})
    return execute


EXPECTED = {
    'init_docx': {'same_document': True, 'rendered': False},
    'render_init': {'same_document': True, 'pic_map': {}, 'index': 1000, 'saved': False, 'part': None},
    '__getattr__': {'text': '{{ value }}', 'same_part': True},
    'render_xml_part': {'xml': '<w:p><w:r><w:t>X</w:t></w:r></w:p>', 'same_part': True},
    'render_properties': {'title': 'X'}, 'render_footnotes': {'text': 'X'},
    'resolve_listing': {'xml': '<w:p><w:r><w:t>A</w:t></w:r><w:r><w:tab/></w:r><w:r><w:t xml:space="preserve">B</w:t><w:br/><w:t xml:space="preserve">C</w:t></w:r></w:p>'},
    'build_xml': {'text': 'X'}, 'map_tree': {'same_body': True, 'text': 'X'},
    'get_headers_footers': {'count': 1, 'same_part': True},
    'get_part_xml': {'text': '{{ value }}', 'declaration': False},
    'get_headers_footers_encoding': {'encodings': ['ISO-8859-1', 'utf-8']},
    'build_headers_footers_xml': {'count': 1, 'bytes': True, 'text': 'X'},
    'map_headers_footers_xml': {'replaced': True, 'same_name': True, 'text': 'X'},
    'fix_tables': {'widths': ['50', '50']},
    'fix_docpr_ids': {'ids': ['1001', '1002'], 'index': 1002},
    'post_processing': {'result': None, 'bytes': 'unchanged', 'position': 2},
    'pre_processing': {'result': None, 'same_document': True},
    'get_pic_map': {'same_map': True, 'value': 1},
}
PROBES = {'api_surface_'+name: Probe(core(name), expected, True) for name, expected in EXPECTED.items()}


def rich_paragraph(engine):
    paragraph = engine.RichTextParagraph('A')
    paragraph.add('B', parastyle='Title')
    return Result({'xml': str(paragraph), 'html': paragraph.__html__(),
                   'alias': engine.RP is engine.RichTextParagraph}, {})


RICH_XML = '<w:p><w:r><w:t xml:space="preserve">A</w:t></w:r></w:p><w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t xml:space="preserve">B</w:t></w:r></w:p>'
PROBES['api_surface_rich_paragraph'] = Probe(rich_paragraph, {'xml': RICH_XML, 'html': RICH_XML, 'alias': True}, True)


def image(engine):
    tpl = engine.DocxTemplate(io.BytesIO(template('image')))
    tpl.render_init(); tpl.current_rendering_part = tpl.docx.part
    buffer = io.BytesIO(); Image.new('RGB', (2, 2)).save(buffer, format='PNG'); buffer.seek(0)
    inline = engine.InlineImage(tpl, buffer)
    xml = str(inline)
    html = inline.__html__()
    relationships = [rel for rel in tpl.docx.part.rels.values() if rel.reltype.endswith('/image')]
    return Result({'drawing': '<w:drawing>' in xml, 'same_html': html == xml, 'images': len(relationships)}, {})


PROBES['api_surface_inline_image'] = Probe(image, {'drawing': True, 'same_html': True, 'images': 1}, True)


def subdoc(engine):
    tpl = engine.DocxTemplate(io.BytesIO(template('parent'))); tpl.render_init()
    child = engine.Subdoc(tpl)
    child.__getattr__('add_paragraph')('child')
    return Result({'xml': str(child), 'html': child.__html__()}, {})


SUBDOC_XML = '<w:p><w:r><w:t>child</w:t></w:r></w:p>'
PROBES['api_surface_subdoc'] = Probe(subdoc, {'xml': SUBDOC_XML, 'html': SUBDOC_XML}, True)


def programmatic_subdoc_type(engine):
    tpl = engine.DocxTemplate(io.BytesIO(template('{{p child }}')))
    child = tpl.new_subdoc()
    return Result({'instance': isinstance(child, engine.Subdoc),
                   'exact_type': type(child) is engine.Subdoc}, {})


PROBES['programmatic_subdoc_type'] = Probe(
    programmatic_subdoc_type, {'instance': True, 'exact_type': True}, True,
)


def programmatic_subdoc_document(engine):
    tpl = engine.DocxTemplate(io.BytesIO(template('{{p child }}')))
    tpl.render_init()
    child = tpl.new_subdoc()
    child.add_paragraph('Programmatic child ✓')
    image = io.BytesIO()
    Image.new('RGB', (8, 6), (23, 42, 81)).save(image, format='PNG')
    image.seek(0)
    child.add_picture(image)
    shared_part = child.subdocx._part is tpl.get_docx()._part
    tpl.render({'child': child})
    output = io.BytesIO()
    tpl.save(output)
    blob = output.getvalue()
    document = Document(io.BytesIO(blob))
    return Result({'shared_part': shared_part,
                   'paragraphs': [p.text for p in document.paragraphs],
                   'images': len(document.inline_shapes)}, {'document.docx': blob})


PROBES['programmatic_subdoc_document'] = Probe(
    programmatic_subdoc_document,
    {'shared_part': True, 'paragraphs': ['Programmatic child ✓', ''], 'images': 1},
)


def mixed_subdoc_document(engine):
    master = Document()
    master.add_paragraph('{{p first }}')
    master.add_paragraph('{{p second }}')
    source = io.BytesIO()
    master.save(source)
    other = Document()
    other.add_paragraph('File child')
    image = io.BytesIO()
    Image.new('RGB', (7, 5), (51, 72, 93)).save(image, format='PNG')
    image.seek(0)
    other.add_picture(image)
    file_source = io.BytesIO()
    other.save(file_source)
    tpl = engine.DocxTemplate(io.BytesIO(source.getvalue()))
    first = tpl.new_subdoc()
    first.add_paragraph('Programmatic child')
    second = tpl.new_subdoc(io.BytesIO(file_source.getvalue()))
    tpl.render({'first': first, 'second': second})
    output = io.BytesIO()
    tpl.save(output)
    blob = output.getvalue()
    document = Document(io.BytesIO(blob))
    return Result({'paragraphs': [p.text for p in document.paragraphs],
                   'images': len(document.inline_shapes)}, {'document.docx': blob})


PROBES['subdoc_mixed_programmatic_file'] = Probe(
    mixed_subdoc_document, {'paragraphs': ['Programmatic child', 'File child', ''], 'images': 1},
)


def subdoc_file_load_timing(engine):
    class ObservedSource(io.BytesIO):
        reads = 0
        def read(self, *args):
            self.reads += 1
            return super().read(*args)
    child = Document()
    child.add_paragraph('Child')
    data = io.BytesIO()
    child.save(data)
    source = ObservedSource(data.getvalue())
    tpl = engine.DocxTemplate(io.BytesIO(template('{{p child }}')))
    tpl.new_subdoc(source)
    return Result({'read_at_creation': source.reads > 0}, {})


PROBES['subdoc_file_load_timing'] = Probe(
    subdoc_file_load_timing, {'read_at_creation': True}, True,
)


def composer(method):
    def execute(engine):
        # Resolve the candidate's own module; never borrow the reference helper.
        cls = importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
        master = Document(); master.add_paragraph('parent')
        child = Document(); child.add_paragraph('child')
        instance = cls(master)
        if method == 'attach_parts':
            result = instance.attach_parts(child)
            observation = {'result': result, 'parent': master.paragraphs[0].text,
                           'child': child.paragraphs[0].text}
        else:
            # No diagram references: the public method must preserve the element.
            element = child.paragraphs[0]._p
            before = etree.tostring(element)
            result = instance.add_diagrams(child, element)
            observation = {'result': result, 'unchanged': etree.tostring(element) == before}
        return Result(observation, {})
    return execute


PROBES['api_surface_composer_attach_parts'] = Probe(composer('attach_parts'), {'result': None, 'parent': 'parent', 'child': 'child'}, True)
PROBES['api_surface_composer_add_diagrams'] = Probe(composer('add_diagrams'), {'result': None, 'unchanged': True}, True)
