"""Differential probes for the scenario partitions of docxtpl and python-docx.

The catalog's rendering (`docxtpl.render.*`), rich value, image, subdocument,
analysis, hyperlink, embedded package and composition contracts, and the
python-docx model contracts (`docx.*`), name partitions specific to each
scenario: undefined values, split tags, header variants, image formats,
retained references, partial failures... Each probe builds the documents of
one partition, renders and saves them, and records what users observe: the
saved document (paragraphs, runs and their formatting, tables, headers and
footers, images, hyperlinks, media, core properties), the exception, the
template's state and the calls the callbacks saw.

Expected observations in ``scenario_partition_expected.json`` were recorded
from the reference (docxtpl 0.20.2) by ``scripts/record_reference_observations.py``.
"""
from __future__ import annotations

import atexit
import gc
import hashlib
import io
import json
import re
import shutil
import tempfile
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import jinja2
from docx import Document
from docx.enum.section import WD_SECTION
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.shared import Emu, Inches, Mm, Pt, RGBColor
from lxml import etree  # ty: ignore[import-not-found]
from markupsafe import Markup

from .probe_images import image
from .protocol_base import Probe, Result

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
NS = {'w': W, 'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
      'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
      'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
HYPERLINK = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink'
PACKAGE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/package'
FOOTNOTES = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes'
_DIRECTORY = tempfile.mkdtemp(prefix='paritylab-scenarios-')
atexit.register(shutil.rmtree, _DIRECTORY, True)


class ProbeError(Exception):
    """An error a callback raises; probes check it reaches the caller as is."""


# Documents ------------------------------------------------------------------

def blob(document: Any) -> bytes:
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def body(*texts: str, style: str | None = None) -> Any:
    document = Document()
    for text in texts:
        document.add_paragraph(text, style=style)
    return document


def runs(*parts: tuple[str, dict[str, Any]]) -> Any:
    document = Document()
    paragraph = document.add_paragraph()
    for text, formatting in parts:
        run = paragraph.add_run(text)
        for name, value in formatting.items():
            if name == 'color':
                run.font.color.rgb = value
            else:
                setattr(run if name in ('bold', 'italic', 'underline') else run.font, name, value)
    return document


def headed(text: str, header: str | None = None, footer: str | None = None) -> Any:
    document = body(text)
    section = document.sections[0]
    if header is not None:
        section.header.paragraphs[0].text = header
    if footer is not None:
        section.footer.paragraphs[0].text = footer
    return document


def table(rows: list[list[str]], style: str | None = None) -> Any:
    document = Document()
    grid = document.add_table(rows=len(rows), cols=max(len(row) for row in rows))
    if style:
        grid.style = style
    for index, row in enumerate(rows):
        for column, text in enumerate(row):
            grid.cell(index, column).text = text
    return document


def png(color: str = 'red', size: tuple[int, int] = (3, 3), format: str = 'PNG') -> bytes:
    return image(color, size, format)


def stored(name: str, data: bytes) -> str:
    path = Path(_DIRECTORY) / name
    path.write_bytes(data)
    return str(path)


def relate_hyperlink(paragraph: Any, url: str, text: str) -> None:
    identity = paragraph.part.relate_to(url, HYPERLINK, is_external=True)
    link = etree.SubElement(paragraph._p, f'{{{W}}}hyperlink')
    link.set(f'{{{NS["r"]}}}id', identity)
    run = etree.SubElement(link, f'{{{W}}}r')
    etree.SubElement(run, f'{{{W}}}t').text = text


def add_footnote(document: Any, paragraph: Any, text: str) -> None:
    xml = (f'<w:footnotes xmlns:w="{W}"><w:footnote w:type="separator" w:id="-1"><w:p/></w:footnote>'
           f'<w:footnote w:id="1"><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:footnote></w:footnotes>')
    part = Part(PackURI('/word/footnotes.xml'),
                'application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml',
                xml.encode(), document.part.package)
    document.part.relate_to(part, FOOTNOTES)
    run = etree.SubElement(paragraph._p, f'{{{W}}}r')
    etree.SubElement(run, f'{{{W}}}footnoteReference').set(f'{{{W}}}id', '1')


def embed(document: Any, name: str, data: bytes) -> None:
    part = Part(PackURI('/word/embeddings/' + name), 'application/octet-stream', data, document.part.package)
    document.part.relate_to(part, PACKAGE)


# Observations ---------------------------------------------------------------

def error(exception: BaseException) -> dict[str, Any]:
    message = re.sub(r' at 0x[0-9a-fA-F]+', '', str(exception)).replace(_DIRECTORY, '<directory>')
    return {'type': type(exception).__module__ + '.' + type(exception).__qualname__, 'message': message}


def attempt(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except Exception as exception:  # noqa: BLE001 - the exception is the observation
        return {'error': error(exception)}


def _runs(paragraph: Any) -> list[list[Any]]:
    def value(item: Any) -> Any:
        return None if item is None else item if isinstance(item, (bool, int, str)) else str(item)
    return [[run.text, run.bold, run.italic, value(run.underline), run.font.strike, value(run.font.color.rgb),
             value(run.font.size), run.font.name, value(run.font.highlight_color), run.font.superscript,
             run.font.subscript, run.style.name if run.style is not None else None]
            for run in paragraph.runs if run.text]


def _cells(grid: Any) -> list[list[Any]]:
    return [[[cell.text, [_cells(inner) for inner in cell.tables]] for cell in row.cells] for row in grid.rows]


def _story(story: Any) -> list[str]:
    return [paragraph.text for paragraph in story.paragraphs] + [
        cell.text for grid in story.tables for row in grid.rows for cell in row.cells]


def observe(data: bytes) -> dict[str, Any]:
    document = Document(io.BytesIO(data))
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        names = package.namelist()
        media = {name: hashlib.sha1(package.read(name)).hexdigest()[:12] for name in names
                 if name.startswith(('word/media/', 'word/embeddings/'))}
        root = etree.fromstring(package.read('word/document.xml'))
        footnotes = (etree.fromstring(package.read('word/footnotes.xml')).xpath('//w:t/text()', namespaces=NS)
                     if 'word/footnotes.xml' in names else None)
    properties = document.core_properties
    sections = []
    for section in document.sections:
        stories = {'header': section.header, 'footer': section.footer}
        if section.different_first_page_header_footer:
            stories |= {'first_header': section.first_page_header, 'first_footer': section.first_page_footer}
        if document.settings.odd_and_even_pages_header_footer:
            stories |= {'even_header': section.even_page_header, 'even_footer': section.even_page_footer}
        sections.append({name: _story(story) for name, story in stories.items() if not story.is_linked_to_previous})
    links = sorted(rel.target_ref for rel in document.part.rels.values() if rel.reltype == HYPERLINK)
    return {
        'paragraphs': [[paragraph.text, paragraph.style.name if paragraph.style is not None else None, _runs(paragraph)]
                       for paragraph in document.paragraphs],
        'tables': [[grid.style.name if grid.style is not None else None, _cells(grid)] for grid in document.tables],
        'sections': sections,
        'images': [[int(shape.width), int(shape.height)] for shape in document.inline_shapes],
        'docpr': root.xpath('//wp:docPr/@id', namespaces=NS),
        'hyperlinks': [links, len(root.xpath('//w:hyperlink | //a:hlinkClick', namespaces=NS))],
        'media': media,
        'footnotes': footnotes,
        'properties': [properties.title, properties.author, properties.subject, properties.keywords,
                       properties.comments, properties.category, properties.content_status],
    }


def load(engine: Any, document: Any) -> Any:
    return engine.DocxTemplate(io.BytesIO(blob(document)))


def outcome(template: Any, *steps: Callable[[], Any]) -> dict[str, Any]:
    """Run `steps`, save, and observe the document or the failure."""
    try:
        for step in steps:
            step()
        output = io.BytesIO()
        template.save(output)
    except Exception as exception:  # noqa: BLE001 - the exception is the observation
        return {'error': error(exception), 'state': [template.is_rendered, template.is_saved]}
    return observe(output.getvalue())


def rendered(engine: Any, document: Any, *contexts: dict[str, Any], **options: Any) -> dict[str, Any]:
    template = load(engine, document)
    return outcome(template, *[lambda context=context: template.render(context, **options) for context in contexts])


Scenario = Callable[[Any], dict[str, Any]]
SCENARIOS: dict[str, dict[str, Scenario]] = {}


def partition(contract: str, name: str) -> Callable[[Scenario], Scenario]:
    def register(scenario: Scenario) -> Scenario:
        SCENARIOS.setdefault(contract, {})[name] = scenario
        return scenario
    return register


# docxtpl.render.body --------------------------------------------------------

@partition('docxtpl.render.body', 'undefined_values')
def _(engine: Any) -> dict[str, Any]:
    document = body('[{{ missing }}][{{ missing.attr }}][{{ none }}][{{ missing|default("d") }}]')
    strict = rendered(engine, body('{{ missing }}'), {}, jinja_env=jinja2.Environment(undefined=jinja2.StrictUndefined))
    chained = rendered(engine, body('{{ missing.attr.deeper }}'), {})
    return {'default': rendered(engine, document, {'none': None}), 'strict': strict, 'chained': chained}


@partition('docxtpl.render.body', 'xml_escaping')
def _(engine: Any) -> dict[str, Any]:
    value = '<a> & "b" \'c\''
    escaped = rendered(engine, body('{{ value }}|{{ safe }}'), {'value': value, 'safe': Markup('<x>')},
                       autoescape=True)
    filtered = rendered(engine, body('{{ value|e }}'), {'value': value})
    plain = rendered(engine, body('{{ value }}'), {'value': '> "q" \'s\''})
    return {'autoescape': escaped, 'filter': filtered, 'plain': plain}


@partition('docxtpl.render.body', 'custom_environments')
def _(engine: Any) -> dict[str, Any]:
    environment = jinja2.Environment(trim_blocks=True, lstrip_blocks=True, finalize=lambda v: '' if v is None else v)
    environment.filters['shout'] = lambda value: str(value).upper() + '!'
    cast(dict[str, Any], environment.globals)['site'] = 'docs'
    environment.tests['short'] = lambda value: len(value) < 3
    return rendered(engine, body('{{ name|shout }} {{ site }} {{ none }}{% if name is short %}S{% endif %}'),
                    {'name': 'ab', 'none': None}, jinja_env=environment)


@partition('docxtpl.render.body', 'complex_objects')
def _(engine: Any) -> dict[str, Any]:
    import datetime
    import decimal

    class Person:
        name = 'Ada'

        def __str__(self) -> str:
            return 'Person(Ada)'

        def greet(self, other: str) -> str:
            return 'hi ' + other
    context = {'mapping': {'k': [1, 2]}, 'items': (1, 'two', 3.5), 'person': Person(),
               'when': datetime.date(2024, 2, 29), 'amount': decimal.Decimal('1.10'), 'flag': True}
    return rendered(engine, body('{{ mapping.k[1] }} {{ mapping["k"]|sum }} {{ items|join("/") }} {{ person }} '
                                 '{{ person.name }} {{ person.greet("x") }} {{ when }} {{ amount }} {{ flag }}'),
                    context)


@partition('docxtpl.render.body', 'error_order')
def _(engine: Any) -> dict[str, Any]:
    trace: list[str] = []

    def first() -> str:
        trace.append('first')
        return 'A'

    def failing() -> str:
        trace.append('failing')
        raise ProbeError('second fails')

    def header() -> str:
        trace.append('header')
        return 'H'
    result = rendered(engine, headed('{{ first() }}{{ failing() }}{{ after() }}', '{{ header() }}'),
                      {'first': first, 'failing': failing, 'header': header, 'after': lambda: trace.append('after')})
    return {'result': result, 'trace': trace}


# docxtpl.render.paragraph_control -------------------------------------------

@partition('docxtpl.render.paragraph_control', 'nested_and_adjacent_controls')
def _(engine: Any) -> dict[str, Any]:
    document = body('{%p if a %}', '{%p if b %}', 'both', '{%p endif %}', '{%p endif %}',
                    '{%p for x in xs %}', 'item {{ x }}', '{%p endfor %}', '{%p if a %}A{%p endif %}', 'end')
    return {'true': rendered(engine, document, {'a': True, 'b': True, 'xs': [1, 2]}),
            'false': rendered(engine, document, {'a': True, 'b': False, 'xs': []})}


@partition('docxtpl.render.paragraph_control', 'split_and_malformed_tags')
def _(engine: Any) -> dict[str, Any]:
    split = runs(('{%', {}), ('p if a ', {'bold': True}), ('%}', {}))
    split.add_paragraph('inside')
    split.add_paragraph('{%p endif %}')
    unclosed = body('{%p if a %}', 'inside')
    stray = body('inside', '{%p endif %}')
    return {'split': rendered(engine, split, {'a': True}), 'unclosed': rendered(engine, unclosed, {'a': True}),
            'stray': rendered(engine, stray, {'a': True})}


@partition('docxtpl.render.paragraph_control', 'formatting_and_relationships')
def _(engine: Any) -> dict[str, Any]:
    document = body('{%p if show %}')
    document.add_paragraph('Title {{ name }}', style='Heading 1')
    paragraph = document.add_paragraph('see ')
    relate_hyperlink(paragraph, 'https://example.com/kept', 'link')
    document.add_paragraph('{%p endif %}')
    document.add_paragraph('{{r rich }}')
    template = load(engine, document)
    rich = engine.RichText('R', url_id=template.build_url_id('https://example.com/rich'), bold=True)
    shown = outcome(template, lambda: template.render({'show': True, 'name': 'N', 'rich': rich}))
    return {'shown': shown, 'hidden': rendered(engine, document, {'show': False, 'rich': ''})}


@partition('docxtpl.render.paragraph_control', 'errors_and_partial_state')
def _(engine: Any) -> dict[str, Any]:
    def condition() -> bool:
        raise ProbeError('condition fails')
    template = load(engine, body('{%p if check() %}', 'inside', '{%p endif %}'))
    failed = outcome(template, lambda: template.render({'check': condition}))
    again = outcome(template, lambda: template.render({'check': lambda: True}))
    return {'failed': failed, 'again': again}


# docxtpl.render.loops -------------------------------------------------------

@partition('docxtpl.render.loops', 'filtered_metadata_and_errors')
def _(engine: Any) -> dict[str, Any]:
    text = '{% for x in xs if x > 1 %}{{ loop.index }}/{{ loop.length }}:{{ x }}{{ "," if not loop.last }}{% endfor %}'
    return {'filtered': rendered(engine, body(text), {'xs': [1, 2, 3, 4]}),
            'error': rendered(engine, body(text), {'xs': [1, 'a']})}


@partition('docxtpl.render.loops', 'nested_and_recursive_loops')
def _(engine: Any) -> dict[str, Any]:
    tree = [{'name': 'a', 'children': [{'name': 'b', 'children': []}, {'name': 'c', 'children': [
        {'name': 'd', 'children': []}]}]}]
    recursive = ('{% for n in tree recursive %}{{ n.name }}{{ loop.depth }}{% if n.children %}[{{ loop(n.children) }}]'
                 '{% endif %}{% endfor %}')
    nested = '{% for r in rows %}{% for c in r %}{{ loop.index }}{{ c }}{% endfor %};{% endfor %}'
    return {'recursive': rendered(engine, body(recursive), {'tree': tree}),
            'nested': rendered(engine, body(nested), {'rows': [['x', 'y'], ['z']]})}


@partition('docxtpl.render.loops', 'iterators_and_mutation')
def _(engine: Any) -> dict[str, Any]:
    items = [1, 2]

    def grow() -> str:
        if len(items) < 4:
            items.append(len(items) + 1)
        return ''
    trace: list[int] = []

    def generator() -> Any:
        for value in range(3):
            trace.append(value)
            yield value
    mutated = rendered(engine, body('{% for x in items %}{{ x }}{{ grow() }}{% endfor %}'),
                       {'items': items, 'grow': grow})
    lazy = rendered(engine, body('{% for x in values %}{{ x }}{% endfor %}|{% for x in values %}{{ x }}{% endfor %}'),
                    {'values': generator()})
    return {'mutated': mutated, 'items': items, 'lazy': lazy, 'trace': trace}


@partition('docxtpl.render.loops', 'else_and_control_flow')
def _(engine: Any) -> dict[str, Any]:
    text = ('{% for x in empty %}a{% else %}empty{% endfor %}|{% for x in xs %}{{ loop.cycle("o", "e") }}'
            '{{ loop.previtem }}{{ loop.nextitem }}{% if loop.changed(x) %}c{% endif %}{% endfor %}')
    return rendered(engine, body(text), {'empty': [], 'xs': [1, 1, 2]})


# docxtpl.render.table_rows --------------------------------------------------

@partition('docxtpl.render.table_rows', 'nested_tables')
def _(engine: Any) -> dict[str, Any]:
    document = table([['outer']])
    inner = document.tables[0].cell(0, 0).add_table(rows=3, cols=1)
    inner.cell(0, 0).text = '{%tr for x in xs %}'
    inner.cell(1, 0).text = '{{ x }}'
    inner.cell(2, 0).text = '{%tr endfor %}'
    return rendered(engine, document, {'xs': ['a', 'b']})


@partition('docxtpl.render.table_rows', 'complex_merge_geometry')
def _(engine: Any) -> dict[str, Any]:
    document = table([['head', ''], ['{%tr for x in xs %}', ''], ['{{ x }}', 'v{{ loop.index }}'],
                      ['{%tr endfor %}', ''], ['foot', '']])
    grid = document.tables[0]
    grid.cell(0, 0).merge(grid.cell(0, 1))
    grid.cell(4, 0).merge(grid.cell(4, 1))
    return rendered(engine, document, {'xs': ['a', 'b']})


@partition('docxtpl.render.table_rows', 'row_styles_and_relationships')
def _(engine: Any) -> dict[str, Any]:
    document = table([['{%tr for x in xs %}'], ['{{r x }}'], ['{%tr endfor %}']], style='Table Grid')
    document.tables[0].cell(1, 0).paragraphs[0].runs[0].bold = True
    template = load(engine, document)
    values = [engine.RichText(name, url_id=template.build_url_id('https://example.com/' + name)) for name in 'ab']
    return outcome(template, lambda: template.render({'xs': values}))


@partition('docxtpl.render.table_rows', 'malformed_controls_and_errors')
def _(engine: Any) -> dict[str, Any]:
    unclosed = table([['{%tr for x in xs %}'], ['{{ x }}']])
    stray = table([['{{ x }}'], ['{%tr endfor %}']])
    failing = table([['{%tr for x in xs %}'], ['{{ x.missing.deeper }}'], ['{%tr endfor %}']])
    return {'unclosed': rendered(engine, unclosed, {'xs': [1]}), 'stray': rendered(engine, stray, {'x': 1}),
            'failing': rendered(engine, failing, {'xs': [1]})}


# docxtpl.render.split_runs --------------------------------------------------

@partition('docxtpl.render.split_runs', 'split_controls_and_comments')
def _(engine: Any) -> dict[str, Any]:
    document = runs(('{{ na', {}), ('me }}', {'italic': True}), (' {# com', {}), ('ment #}', {}),
                    (' {%', {}), (' if a %}yes{% en', {'bold': True}), ('dif %}', {}))
    return rendered(engine, document, {'name': 'N', 'a': True})


@partition('docxtpl.render.split_runs', 'unicode_and_run_formatting')
def _(engine: Any) -> dict[str, Any]:
    document = runs(('façade {{ naï', {'bold': True}), ('ve_ascii }} 日本', {'italic': True, 'size': Pt(14)}),
                    (' {{ emoji }}', {'color': RGBColor(0xFF, 0, 0)}))
    return rendered(engine, document, {'naïve_ascii': 'yes', 'emoji': '🙂 é'})


@partition('docxtpl.render.split_runs', 'xml_boundaries')
def _(engine: Any) -> dict[str, Any]:
    document = runs(('{{ va', {}), ('lue }}', {}), (' tail', {}))
    paragraph = document.paragraphs[0]._p
    first = paragraph.findall(f'{{{W}}}r')[0]
    first.addnext(etree.fromstring(f'<w:proofErr xmlns:w="{W}" w:type="spellStart"/>'))
    first.addprevious(etree.fromstring(f'<w:bookmarkStart xmlns:w="{W}" w:id="0" w:name="mark"/>'))
    paragraph.append(etree.fromstring(f'<w:bookmarkEnd xmlns:w="{W}" w:id="0"/>'))
    return rendered(engine, document, {'value': 'V'})


@partition('docxtpl.render.split_runs', 'malformed_tags_and_errors')
def _(engine: Any) -> dict[str, Any]:
    unclosed = runs(('{{ name', {}), (' }', {}))
    unknown = runs(('{% unknown', {}), (' %}', {}))
    return {'unclosed': rendered(engine, unclosed, {'name': 'N'}), 'unknown': rendered(engine, unknown, {})}


# docxtpl.render.headers -----------------------------------------------------

def _sections() -> Any:
    document = headed('body {{ value }}', 'default {{ value }}', 'foot {{ value }}')
    first = document.sections[0]
    first.different_first_page_header_footer = True
    first.first_page_header.paragraphs[0].text = 'first {{ value }}'
    document.settings.odd_and_even_pages_header_footer = True
    first.even_page_header.paragraphs[0].text = 'even {{ value }}'
    second = document.add_section(WD_SECTION.NEW_PAGE)
    second.header.is_linked_to_previous = False
    second.header.paragraphs[0].text = 'second {{ value }}'
    document.add_paragraph('after {{ value }}')
    return document


@partition('docxtpl.render.headers', 'multiple_sections_and_variants')
def _(engine: Any) -> dict[str, Any]:
    return rendered(engine, _sections(), {'value': 'V'})


@partition('docxtpl.render.headers', 'part_references_and_attachments')
def _(engine: Any) -> dict[str, Any]:
    document = headed('body', '{{ image }} {{r link }}')
    template = load(engine, document)
    image = engine.InlineImage(template, io.BytesIO(png('blue')), width=Mm(5))
    return outcome(template, lambda: template.render({'image': image, 'link': engine.RichText('L')}))


@partition('docxtpl.render.headers', 'mutation_and_error_order')
def _(engine: Any) -> dict[str, Any]:
    trace: list[str] = []

    def mark(name: str) -> Callable[[], str]:
        return lambda: trace.append(name) or name
    ordered = rendered(engine, headed('{{ body() }}', '{{ header() }}', '{{ footer() }}'),
                       {'body': mark('body'), 'header': mark('header'), 'footer': mark('footer')})
    failing_trace: list[str] = []

    def fail() -> str:
        failing_trace.append('header')
        raise ProbeError('header fails')
    template = load(engine, headed('{{ body() }}', '{{ fail() }}', '{{ footer() }}'))
    failed = outcome(template, lambda: template.render({'body': lambda: failing_trace.append('body') or 'B',
                                                        'fail': fail,
                                                        'footer': lambda: failing_trace.append('footer') or 'F'}))
    return {'ordered': ordered, 'trace': trace, 'failed': failed, 'failing_trace': failing_trace}


@partition('docxtpl.render.headers', 'formatting_and_xml_edges')
def _(engine: Any) -> dict[str, Any]:
    document = headed('body')
    header = document.sections[0].header
    paragraph = header.paragraphs[0]
    paragraph.add_run('{{ na').bold = True
    paragraph.add_run('me }}').italic = True
    grid = header.add_table(rows=1, cols=1, width=Inches(2))
    grid.cell(0, 0).text = '{{ value }}'
    return rendered(engine, document, {'name': '"N" > M', 'value': Markup('<b>')}, autoescape=True)


# docxtpl.rich.* -------------------------------------------------------------

@partition('docxtpl.rich.inline', 'arbitrary_value_protocols')
def _(engine: Any) -> dict[str, Any]:
    class Named:
        def __str__(self) -> str:
            return 'named'
    rich = engine.RichText(Named())
    rich.add(12)
    rich.add(3.5, bold=True)
    rich.add(None)
    rich.add(engine.RichText('inner', italic=True))
    return rendered(engine, body('{{r rich }}'), {'rich': rich})


@partition('docxtpl.rich.inline', 'unicode_and_xml_edges')
def _(engine: Any) -> dict[str, Any]:
    rich = engine.RichText('a\nb\tc\fd\ae <&> "q"')
    rich.add(' 日本 🙂 \u202eltr', rtl=True, lang='he-IL')
    return rendered(engine, body('{{r rich }}'), {'rich': rich})


@partition('docxtpl.rich.inline', 'formatting_interactions')
def _(engine: Any) -> dict[str, Any]:
    rich = engine.RichText('all', style='Strong', color='#00FF00', highlight='yellow', size=20, bold=True,
                           italic=True, underline='double', strike=True, font='Arial')
    rich.add('sub', subscript=True, color='FF0000')
    rich.add('sup', superscript=True, underline=True)
    rich.add('plain')
    plain_tag = rendered(engine, body('{{ rich }}'), {'rich': engine.RichText('in plain tag', bold=True)})
    return {'run_tag': rendered(engine, runs(('x {{r rich }} y', {'italic': True})), {'rich': rich}),
            'plain_tag': plain_tag}


@partition('docxtpl.rich.inline', 'errors_and_identity')
def _(engine: Any) -> dict[str, Any]:
    rich = engine.RichText('R')
    paragraph_tag = rendered(engine, body('{{p rich }}'), {'rich': rich})
    bad_size = attempt(lambda: rendered(engine, body('{{r rich }}'), {'rich': engine.RichText('x', size='big')}))
    return {'paragraph_tag': paragraph_tag, 'bad_size': bad_size, 'same': rich.xml == engine.RichText('R').xml}


@partition('docxtpl.rich.identity', 'subclasses_and_user_protocols')
def _(engine: Any) -> dict[str, Any]:
    class Loud(engine.RichText):
        def add(self, text: Any, *args: Any, **kwargs: Any) -> Any:
            return super().add(str(text).upper(), *args, **kwargs)
    loud = Loud('quiet ')
    loud.add('shout')
    return rendered(engine, body('{{r loud }}|{{ loud.__class__.__name__ }}'), {'loud': loud})


@partition('docxtpl.rich.identity', 'shared_references_and_cycles')
def _(engine: Any) -> dict[str, Any]:
    rich = engine.RichText('R', bold=True)
    rich.add(rich)
    return rendered(engine, body('{{r rich }}', '{{r rich }}', '{{r items[0] }}{{r items[1] }}'),
                    {'rich': rich, 'items': [rich, rich]})


@partition('docxtpl.rich.identity', 'multiple_parts_and_nested_rendering')
def _(engine: Any) -> dict[str, Any]:
    rich = engine.RichText('shared', italic=True)

    def nested() -> str:
        inner = load(engine, body('{{r rich }}'))
        inner.render({'rich': rich})
        return 'nested ' + str(observe(blob(inner))['paragraphs'][0][0])
    return rendered(engine, headed('{{r rich }} {{ nested() }}', '{{r rich }}', '{{r rich }}'),
                    {'rich': rich, 'nested': nested})


@partition('docxtpl.rich.identity', 'failure_and_mutation_order')
def _(engine: Any) -> dict[str, Any]:
    rich = engine.RichText('first')

    def mutate() -> str:
        rich.add(' added')
        return ''

    def fail() -> str:
        raise ProbeError('after mutation')
    mutated = rendered(engine, body('{{r rich }}{{ mutate() }}{{r rich }}'), {'rich': rich, 'mutate': mutate})
    failed = rendered(engine, body('{{ mutate() }}{{ fail() }}'), {'mutate': mutate, 'fail': fail})
    return {'mutated': mutated, 'failed': failed, 'xml_runs': str(rich).count('<w:r>')}


@partition('docxtpl.rich.mutation', 'mutation_across_parts')
def _(engine: Any) -> dict[str, Any]:
    rich = engine.RichText('body')

    def mutate() -> str:
        rich.add(' then header', bold=True)
        return ''
    return rendered(engine, headed('{{r rich }}{{ mutate() }}', '{{r rich }}', '{{r rich }}'),
                    {'rich': rich, 'mutate': mutate})


@partition('docxtpl.rich.mutation', 'shared_values_and_cycles')
def _(engine: Any) -> dict[str, Any]:
    first, second = engine.RichText('1'), engine.RichText('2')
    first.add(second)
    second.add(first)
    second.add('!')
    return rendered(engine, body('{{r first }}|{{r second }}'), {'first': first, 'second': second})


@partition('docxtpl.rich.mutation', 'subclass_protocols')
def _(engine: Any) -> dict[str, Any]:
    class Counted(engine.RichText):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.calls = 0
            super().__init__(*args, **kwargs)

        def __str__(self) -> str:
            self.calls += 1
            return super().__str__()

        def __html__(self) -> str:
            self.calls += 1
            return super().__html__()
    value = Counted('counted')
    result = rendered(engine, body('{{r value }} {{ value }}'), {'value': value})
    return {'result': result, 'calls': value.calls > 0}


@partition('docxtpl.rich.mutation', 'exceptions_and_partial_state')
def _(engine: Any) -> dict[str, Any]:
    class Failing:
        def __str__(self) -> str:
            raise ProbeError('text fails')
    rich = engine.RichText('kept')
    try:
        rich.add(Failing(), bold=True)
        raised = None
    except ProbeError as exception:
        raised = error(exception)
    return {'raised': raised, 'result': rendered(engine, body('{{r rich }}'), {'rich': rich})}


@partition('docxtpl.rich.mutation', 'nested_rendering')
def _(engine: Any) -> dict[str, Any]:
    rich = engine.RichText('outer')

    def inner() -> str:
        nested = load(engine, body('{{r rich }}'))
        rich.add(' inner')
        nested.render({'rich': rich})
        return observe(blob(nested))['paragraphs'][0][0]
    return rendered(engine, body('{{ inner() }}|{{r rich }}'), {'rich': rich, 'inner': inner})


# docxtpl.image.* ------------------------------------------------------------

def _image(engine: Any, data: Any, text: str = '{{ image }}', **options: Any) -> dict[str, Any]:
    template = load(engine, body(text))
    image = engine.InlineImage(template, io.BytesIO(data) if isinstance(data, bytes) else data, **options)
    return outcome(template, lambda: template.render({'image': image}))


@partition('docxtpl.image.basic', 'image_formats_and_corruption')
def _(engine: Any) -> dict[str, Any]:
    formats = {name: _image(engine, png('green', format=name)) for name in ('PNG', 'JPEG', 'GIF', 'BMP', 'TIFF')}
    return formats | {'corrupt': _image(engine, b'not an image'), 'truncated': _image(engine, png()[:20])}


@partition('docxtpl.image.basic', 'dimensions_and_rounding')
def _(engine: Any) -> dict[str, Any]:
    data = png(size=(30, 20))
    return {'width': _image(engine, data, width=Mm(10.3)), 'height': _image(engine, data, height=Inches(0.37)),
            'both': _image(engine, data, width=Emu(12345), height=Pt(7.5)), 'none': _image(engine, data),
            'float': _image(engine, data, width=Mm(3) + 0.5)}


@partition('docxtpl.image.basic', 'path_and_stream_protocols')
def _(engine: Any) -> dict[str, Any]:
    path = stored('image.png', png('purple'))
    stream = io.BytesIO(b'xx' + png('purple'))
    stream.seek(2)
    return {'str': _image(engine, path), 'path': _image(engine, Path(path)), 'offset': _image(engine, stream)}


@partition('docxtpl.image.basic', 'hyperlink_and_relationships')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, body('{{ a }}{{ b }}{{ c }}'))
    data = png('orange')
    a = engine.InlineImage(template, io.BytesIO(data), anchor='https://example.com/a', title='A', descr='first')
    b = engine.InlineImage(template, io.BytesIO(data), anchor='https://example.com/a')
    c = engine.InlineImage(template, io.BytesIO(png('black')))
    return outcome(template, lambda: template.render({'a': a, 'b': b, 'c': c}))


@partition('docxtpl.image.basic', 'errors_and_mutation')
def _(engine: Any) -> dict[str, Any]:
    missing = _image(engine, str(Path(_DIRECTORY) / 'missing.png'))
    template = load(engine, body('{{ image }}'))
    image = engine.InlineImage(template, io.BytesIO(png()), width=Mm(5))
    image.width = Mm(9)
    image.title = 'changed'
    return {'missing': missing, 'mutated': outcome(template, lambda: template.render({'image': image}))}


@partition('docxtpl.image.live_relationship', 'multiple_parts_and_sections')
def _(engine: Any) -> dict[str, Any]:
    document = headed('{{ image }}', '{{ image }}', '{{ image }}')
    template = load(engine, document)
    image = engine.InlineImage(template, io.BytesIO(png('teal')), width=Mm(4))
    return outcome(template, lambda: template.render({'image': image}))


@partition('docxtpl.image.live_relationship', 'relationship_deduplication')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, body('{{ a }}{{ b }}{{ a }}'))
    data = png('navy')
    a = engine.InlineImage(template, io.BytesIO(data))
    b = engine.InlineImage(template, io.BytesIO(data), width=Mm(3))
    return outcome(template, lambda: template.render({'a': a, 'b': b}))


@partition('docxtpl.image.live_relationship', 'mutation_and_retained_references')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, body('{{ image }}'))
    image = engine.InlineImage(template, io.BytesIO(png('lime')))
    first = outcome(template, lambda: template.render({'image': image}))
    image.width = Mm(6)
    second = outcome(template, lambda: template.render({'image': image}))
    return {'first': first, 'second': second}


@partition('docxtpl.image.live_relationship', 'errors_and_partial_state')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, body('{{ good }}{{ bad }}'))
    good = engine.InlineImage(template, io.BytesIO(png('gold')))
    bad = engine.InlineImage(template, io.BytesIO(b'corrupt'))
    failed = outcome(template, lambda: template.render({'good': good, 'bad': bad}))
    retried = outcome(template, lambda: template.render({'good': good, 'bad': ''}))
    return {'failed': failed, 'retried': retried}


@partition('docxtpl.image.live_relationship', 'nested_rendering')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, headed('{{ make() }}', '{{ make() }}'))

    def make() -> Any:
        return engine.InlineImage(template, io.BytesIO(png('maroon')), width=Mm(2))
    return outcome(template, lambda: template.render({'make': make}))


# docxtpl.subdoc.* -----------------------------------------------------------

def _subdocument(name: str, build: Callable[[Any], None]) -> str:
    document = Document()
    build(document)
    return stored(name, blob(document))


@partition('docxtpl.subdoc.file', 'stream_lifecycle_and_errors')
def _(engine: Any) -> dict[str, Any]:
    path = _subdocument('stream.docx', lambda document: document.add_paragraph('from file'))
    template = load(engine, body('{{p sub }}'))
    loaded = outcome(template, lambda: template.render({'sub': engine.Subdoc(template, path)}))
    stream = io.BytesIO(Path(path).read_bytes())
    streamed = load(engine, body('{{p sub }}'))
    from_stream = outcome(streamed, lambda: streamed.render({'sub': engine.Subdoc(streamed, stream)}))
    missing = load(engine, body('{{p sub }}'))
    absent = outcome(missing, lambda: engine.Subdoc(missing, str(Path(_DIRECTORY) / 'absent.docx')))
    return {'path': loaded, 'stream': from_stream, 'missing': absent}


@partition('docxtpl.subdoc.file', 'complex_relationship_graph')
def _(engine: Any) -> dict[str, Any]:
    def build(document: Any) -> None:
        paragraph = document.add_paragraph('with ')
        relate_hyperlink(paragraph, 'https://example.com/sub', 'link')
        document.add_picture(io.BytesIO(png('cyan')), width=Mm(4))
        document.add_picture(io.BytesIO(png('cyan')), width=Mm(5))
    path = _subdocument('graph.docx', build)
    document = body('{{p sub }}')
    document.add_picture(io.BytesIO(png('magenta')), width=Mm(3))
    template = load(engine, document)
    return outcome(template, lambda: template.render({'sub': engine.Subdoc(template, path)}))


@partition('docxtpl.subdoc.file', 'styles_notes_sections_and_properties')
def _(engine: Any) -> dict[str, Any]:
    def build(document: Any) -> None:
        document.add_paragraph('Heading', style='Heading 1')
        document.add_paragraph('one', style='List Number')
        document.add_paragraph('two', style='List Number')
        grid = document.add_table(rows=1, cols=1)
        grid.style = 'Light Grid Accent 1'
        grid.cell(0, 0).text = 'cell'
        add_footnote(document, document.add_paragraph('noted'), 'the note')
        document.add_section(WD_SECTION.NEW_PAGE)
        document.add_paragraph('second section')
        document.core_properties.title = 'subdoc title'
    path = _subdocument('styles.docx', build)
    template = load(engine, body('{{p sub }}', 'tail'))
    return outcome(template, lambda: template.render({'sub': engine.Subdoc(template, path)}))


@partition('docxtpl.subdoc.file', 'repeated_and_nested_rendering')
def _(engine: Any) -> dict[str, Any]:
    path = _subdocument('repeat.docx', lambda document: document.add_paragraph('repeat {{ ignored }}'))
    template = load(engine, body('{{p sub }}', '{{p sub }}'))
    sub = engine.Subdoc(template, path)
    first = outcome(template, lambda: template.render({'sub': sub}))
    second = outcome(template, lambda: template.render({'sub': sub}))
    return {'first': first, 'second': second}


@partition('docxtpl.subdoc.file', 'mutations_and_retained_references')
def _(engine: Any) -> dict[str, Any]:
    path = _subdocument('mutable.docx', lambda document: document.add_paragraph('original'))
    template = load(engine, body('{{p sub }}'))
    sub = engine.Subdoc(template, path)
    Path(path).write_bytes(blob(body('rewritten')))
    sub.add_paragraph('appended')
    return outcome(template, lambda: template.render({'sub': sub}))


@partition('docxtpl.subdoc.builder', 'callbacks_and_error_identity')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, body('{{p make() }}'))

    def make() -> Any:
        sub = template.new_subdoc()
        sub.add_paragraph('made in callback')
        return sub
    made = outcome(template, lambda: template.render({'make': make}))
    raised = ProbeError('builder fails')
    failing = load(engine, body('{{p make() }}'))
    caught: list[bool] = []

    def fail() -> Any:
        raise raised
    try:
        failing.render({'make': fail})
    except ProbeError as exception:
        caught.append(exception is raised)
    return {'made': made, 'same_exception': caught}


@partition('docxtpl.subdoc.builder', 'object_mutations_and_lifetime')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, body('{{p sub }}'))
    sub = template.new_subdoc()
    paragraph = sub.add_paragraph('first')
    context = {'sub': sub}
    paragraph.add_run(' more').bold = True
    sub.add_paragraph('second')
    result = outcome(template, lambda: template.render(context))
    return {'result': result, 'retained': paragraph.text}


@partition('docxtpl.subdoc.builder', 'relationships_and_parts')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, body('{{p sub }}'))
    sub = template.new_subdoc()
    paragraph = sub.add_paragraph('link ')
    relate_hyperlink(paragraph, 'https://example.com/builder', 'here')
    sub.add_picture(io.BytesIO(png('pink')), width=Mm(3))
    return outcome(template, lambda: template.render({'sub': sub}))


@partition('docxtpl.subdoc.builder', 'nested_and_repeated_rendering')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, body('{{p sub }}', '{{p sub }}'))
    sub = template.new_subdoc()
    sub.add_paragraph('twice')
    first = outcome(template, lambda: template.render({'sub': sub}))
    second = outcome(template, lambda: template.render({'sub': sub}))
    return {'first': first, 'second': second}


# docxtpl.analysis.undeclared ------------------------------------------------

def _undeclared(engine: Any, document: Any, **options: Any) -> dict[str, Any]:
    template = load(engine, document)
    try:
        return {'value': sorted(template.get_undeclared_template_variables(**options)),
                'state': [template.is_rendered, template.is_saved]}
    except Exception as exception:  # noqa: BLE001 - the exception is the observation
        return {'error': error(exception)}


@partition('docxtpl.analysis.undeclared', 'parser_errors_and_identity')
def _(engine: Any) -> dict[str, Any]:
    return {'syntax': _undeclared(engine, body('{% if %}')), 'unclosed': _undeclared(engine, body('{{ name ')),
            'tag': _undeclared(engine, body('{%p if a %}', 'x'))}


@partition('docxtpl.analysis.undeclared', 'context_protocol_and_errors')
def _(engine: Any) -> dict[str, Any]:
    from collections import UserDict
    document = body('{{ a }} {{ b }} {{ c.d }}')
    return {'dict': _undeclared(engine, document, context={'a': 1}),
            'mapping': _undeclared(engine, document, context=UserDict({'b': 2})),
            'none': _undeclared(engine, document, context=None),
            'list': _undeclared(engine, document, context=['a'])}


@partition('docxtpl.analysis.undeclared', 'custom_relationships_and_mutation')
def _(engine: Any) -> dict[str, Any]:
    document = headed('{{ body_name }}', '{{ header_name }}', '{{ footer_name }}')
    document.core_properties.title = '{{ title_name }}'
    template = load(engine, document)
    first = sorted(template.get_undeclared_template_variables())
    template.render({'body_name': 'b', 'header_name': 'h', 'footer_name': 'f', 'title_name': 't'})
    after = sorted(template.get_undeclared_template_variables())
    return {'first': first, 'after': after, 'state': [template.is_rendered, template.is_saved]}


@partition('docxtpl.analysis.undeclared', 'scope_and_extension_semantics')
def _(engine: Any) -> dict[str, Any]:
    text = ('{% set local = outer %}{% for item in items %}{{ item }}{{ loop.index }}{% endfor %}'
            '{% macro m(arg) %}{{ arg }}{{ free }}{% endmacro %}{{ m(1) }}{% with w = with_value %}{{ w }}{% endwith %}'
            '{% do items.append(1) %}{{ local }}')
    return {'extension': _undeclared(engine, body(text), jinja_env=jinja2.Environment(extensions=['jinja2.ext.do'])),
            'default': _undeclared(engine, body(text))}


# docxtpl.url.ids ------------------------------------------------------------

@partition('docxtpl.url.ids', 'concurrent_and_reentrant_calls')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, body('{{r first() }}{{r second() }}'))
    ids: list[str] = []

    def link(url: str) -> Callable[[], Any]:
        def make() -> Any:
            identity = template.build_url_id(url)
            ids.append(identity)
            return engine.RichText(url, url_id=identity)
        return make
    before = [template.build_url_id('https://example.com/a'), template.build_url_id('https://example.com/a')]
    result = outcome(template, lambda: template.render({'first': link('https://example.com/a'),
                                                        'second': link('https://example.com/b')}))
    return {'before': before, 'during': ids, 'result': result}


@partition('docxtpl.url.ids', 'custom_target_protocols')
def _(engine: Any) -> dict[str, Any]:
    class Url(str):
        pass
    template = load(engine, body('{{r a }}{{r b }}{{r c }}'))
    a = template.build_url_id(Url('https://example.com/sub'))
    b = template.build_url_id('https://example.com/é?q=1&r=<2>')
    c = template.build_url_id('mailto:someone@example.com')
    values = {name: engine.RichText(name, url_id=identity) for name, identity in zip('abc', (a, b, c), strict=True)}
    return {'ids': [a, b, c], 'result': outcome(template, lambda: template.render(values))}


@partition('docxtpl.url.ids', 'multiple_parts_and_existing_relationships')
def _(engine: Any) -> dict[str, Any]:
    document = headed('{{r link }}', '{{r link }}')
    relate_hyperlink(document.paragraphs[0], 'https://example.com/existing', 'old')
    template = load(engine, document)
    existing = template.build_url_id('https://example.com/existing')
    new = template.build_url_id('https://example.com/new')
    rich = engine.RichText('new', url_id=new)
    return {'ids': [existing, new], 'result': outcome(template, lambda: template.render({'link': rich}))}


@partition('docxtpl.url.ids', 'error_and_mutation_order')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, body('{{r link }}'))
    first = template.build_url_id('https://example.com/1')
    template.render({'link': engine.RichText('one', url_id=first)})
    output = io.BytesIO()
    template.save(output)
    after_save = template.build_url_id('https://example.com/2')
    result = outcome(template, lambda: template.render({'link': engine.RichText('two', url_id=after_save)}))
    return {'first': observe(output.getvalue()), 'ids': [first, after_save], 'result': result}


# docxtpl.package.embedded ---------------------------------------------------

@partition('docxtpl.package.embedded', 'other_embedded_types_and_errors')
def _(engine: Any) -> dict[str, Any]:
    document = body('embedded')
    embed(document, 'sheet.xlsx', b'sheet payload')
    embed(document, 'object.bin', b'binary payload')
    source = blob(document)
    old_sheet, new_sheet = stored('old_sheet.xlsx', b'sheet payload'), stored('new_sheet.xlsx', b'new sheet')
    old_bin, new_bin = stored('old.bin', b'binary payload'), stored('new.bin', b'new binary')
    unmatched = stored('unmatched.bin', b'no such payload')
    template = engine.DocxTemplate(io.BytesIO(source))
    replaced = outcome(template, lambda: template.replace_embedded(old_sheet, new_sheet),
                       lambda: template.replace_embedded(old_bin, new_bin),
                       lambda: template.replace_embedded(unmatched, new_bin), lambda: template.render({}))
    missing = engine.DocxTemplate(io.BytesIO(source))
    absent = outcome(missing, lambda: missing.replace_embedded(old_sheet, str(Path(_DIRECTORY) / 'absent.bin')))
    return {'replaced': replaced, 'missing': absent}


# docxtpl.composition.merge --------------------------------------------------

def _compose(engine: Any, build: Callable[[Any], None], name: str) -> dict[str, Any]:
    path = _subdocument(name, build)
    template = load(engine, body('before', '{{p sub }}', 'after'))
    return outcome(template, lambda: template.render({'sub': engine.Subdoc(template, path)}))


@partition('docxtpl.composition.merge', 'all_relationship_types')
def _(engine: Any) -> dict[str, Any]:
    def build(document: Any) -> None:
        relate_hyperlink(document.add_paragraph('link '), 'https://example.com/merged', 'here')
        document.add_picture(io.BytesIO(png('olive')), width=Mm(4))
        add_footnote(document, document.add_paragraph('note'), 'merged note')
        document.sections[0].header.paragraphs[0].text = 'sub header'
    return _compose(engine, build, 'relationships.docx')


@partition('docxtpl.composition.merge', 'styles_numbering_and_sections')
def _(engine: Any) -> dict[str, Any]:
    def build(document: Any) -> None:
        document.add_paragraph('bullet', style='List Bullet')
        document.add_paragraph('number', style='List Number')
        document.add_paragraph('number 2', style='List Number')
        document.add_paragraph('quote', style='Intense Quote')
        document.add_section(WD_SECTION.ODD_PAGE)
        document.add_paragraph('landscape section')
    return _compose(engine, build, 'numbering.docx')


@partition('docxtpl.composition.merge', 'notes_and_properties')
def _(engine: Any) -> dict[str, Any]:
    def build(document: Any) -> None:
        add_footnote(document, document.add_paragraph('with note'), 'composed note')
        document.core_properties.title = 'sub title'
        document.core_properties.author = 'sub author'
    return _compose(engine, build, 'notes.docx')


@partition('docxtpl.composition.merge', 'invalid_packages_and_partial_failure')
def _(engine: Any) -> dict[str, Any]:
    not_zip = stored('not_zip.docx', b'plain bytes')
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, 'w') as package:
        package.writestr('other.txt', 'x')
    no_document = stored('no_document.docx', empty.getvalue())
    results = {}
    for name, path in (('not_zip', not_zip), ('no_document', no_document)):
        template = load(engine, body('{{p sub }}'))
        results[name] = outcome(template, lambda template=template, path=path: engine.Subdoc(template, path))
    return results


@partition('docxtpl.composition.merge', 'formatting_and_visual_stability')
def _(engine: Any) -> dict[str, Any]:
    def build(document: Any) -> None:
        paragraph = document.add_paragraph()
        run = paragraph.add_run('styled')
        run.bold, run.italic = True, True
        run.font.color.rgb = RGBColor(0x12, 0x34, 0x56)
        run.font.size = Pt(15)
        run.font.name = 'Courier New'
        grid = document.add_table(rows=2, cols=2)
        grid.style = 'Table Grid'
        grid.cell(0, 0).merge(grid.cell(0, 1)).text = 'merged'
    return _compose(engine, build, 'formatting.docx')


# docx.document.* ------------------------------------------------------------

def _types(model: Any) -> dict[str, Any]:
    return {'document': type(model).__name__, 'part': type(model.part).__name__,
            'element': type(model.element).__name__,
            'targets': sorted([rel.reltype.rsplit('/', 1)[-1], type(rel.target_part).__name__]
                              for rel in model.part.rels.values() if not rel.is_external)}


@partition('docx.document.types', 'part_and_relationship_types')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, headed('{{ value }}', '{{ value }}', '{{ value }}'))
    before = _types(template.get_docx())
    template.render({'value': 'V'})
    return {'before': before, 'after': _types(template.get_docx())}


@partition('docx.document.types', 'subclasses_and_user_proxies')
def _(engine: Any) -> dict[str, Any]:
    trace: list[str] = []

    class Custom(engine.DocxTemplate):
        def get_docx(self) -> Any:
            trace.append('get_docx')
            return super().get_docx()

    class Proxy:
        def __init__(self, target: Any) -> None:
            self._target = target

        def __getattr__(self, name: str) -> Any:
            return getattr(self._target, name)
    template = Custom(io.BytesIO(blob(body('{{ proxy.text }} {{ sub }}'))))
    model = template.get_docx()
    result = outcome(template, lambda: template.render({'proxy': Proxy(model.paragraphs[0]),
                                                        'sub': type(template.new_subdoc()).__name__}))
    return {'result': result, 'trace': trace}


@partition('docx.document.types', 'mutation_and_object_lifetime')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, body('{{ value }}', 'second'))
    model = template.get_docx()
    first, second = model.paragraphs[0], model.paragraphs[1]
    second.add_run(' extended')
    template.render({'value': 'V'})
    after = template.get_docx()
    return {'same_model': after is model, 'retained': [first.text, second.text],
            'current': [paragraph.text for paragraph in after.paragraphs], 'output': observe(blob(template))}


@partition('docx.document.types', 'thread_and_reentrancy')
def _(engine: Any) -> dict[str, Any]:
    results: dict[str, Any] = {}

    def run() -> None:
        results['thread'] = rendered(engine, body('{{ value }}'), {'value': 'threaded'})
    worker = threading.Thread(target=run)
    worker.start()
    worker.join()

    def inner() -> str:
        return rendered(engine, body('{{ value }}'), {'value': 'inner'})['paragraphs'][0][0]
    results['reentrant'] = rendered(engine, body('{{ inner() }}'), {'inner': inner})
    return results


@partition('docx.document.retained', 'headers_footers_and_parts')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, headed('{{ value }}', 'head {{ value }}', 'foot {{ value }}'))
    model = template.get_docx()
    header, footer = model.sections[0].header.paragraphs[0], model.sections[0].footer.paragraphs[0]
    header.runs[0].bold = True
    template.render({'value': 'V'})
    return {'retained': [header.text, footer.text], 'output': observe(blob(template))}


@partition('docx.document.retained', 'multiple_renders_and_saves')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, body('{{ value }}'))
    paragraph = template.get_docx().paragraphs[0]
    template.render({'value': 'one'})
    first = observe(blob(template))
    template.render({'value': 'two'})
    second = observe(blob(template))
    return {'first': first, 'second': second, 'retained': paragraph.text,
            'state': [template.is_rendered, template.is_saved]}


@partition('docx.document.retained', 'nested_mutations_and_relationships')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, body('{{ change() }}', 'kept'))
    model = template.get_docx()
    kept = model.paragraphs[1]

    def change() -> str:
        kept.add_run(' changed').italic = True
        relate_hyperlink(kept, 'https://example.com/nested', 'nested')
        return 'C'
    result = outcome(template, lambda: template.render({'change': change}))
    return {'result': result, 'retained': kept.text}


@partition('docx.document.retained', 'errors_and_object_lifetime')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, body('{{ value }}{{ fail() }}'))
    model = template.get_docx()
    paragraph = model.paragraphs[0]

    def fail() -> str:
        raise ProbeError('render fails')
    failed = outcome(template, lambda: template.render({'value': 'V', 'fail': fail}))
    return {'failed': failed, 'retained': paragraph.text, 'same_model': template.docx is model,
            'saved': outcome(template)}


def _header_parts(model: Any) -> list[Any]:
    return [rel.target_part for rel in model.part.rels.values()
            if rel.reltype.endswith(('/header', '/footer'))]


@partition('docx.document.header_commit', 'multiple_sections_and_variants')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, _sections())
    model = template.get_docx()
    before = _header_parts(model)
    template.render({'value': 'V'})
    after = _header_parts(model)
    return {'replaced': [new is not old for old, new in zip(before, after, strict=True)],
            'output': observe(blob(template))}


@partition('docx.document.header_commit', 'footer_and_other_parts')
def _(engine: Any) -> dict[str, Any]:
    document = headed('body', None, 'footer {{ value }}')
    add_footnote(document, document.paragraphs[0], 'note {{ value }}')
    template = load(engine, document)
    model = template.get_docx()
    footer = model.sections[0].footer.part
    template.render({'value': 'V'})
    return {'replaced': model.sections[0].footer.part is not footer, 'output': observe(blob(template))}


@partition('docx.document.header_commit', 'relationship_identity')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, headed('body', '{{ value }}', '{{ value }}'))
    model = template.get_docx()
    before = {identity: rel.reltype for identity, rel in model.part.rels.items()}
    template.render({'value': 'V'})
    after = {identity: rel.reltype for identity, rel in model.part.rels.items()}
    return {'same': before == after, 'ids': sorted(after), 'output': observe(blob(template))}


@partition('docx.document.header_commit', 'partial_failure_and_callbacks')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, headed('{{ value }}', 'one {{ value }}', '{{ fail() }}'))
    model = template.get_docx()

    def fail() -> str:
        raise ProbeError('footer fails')
    kept: list[BaseException] = []
    try:
        template.render({'value': 'V', 'fail': fail})
    except ProbeError as exception:
        kept.append(exception)
    # docxtpl's render holds the parsed body in a local: while the failure's
    # traceback lives, the model's body is that plain lxml element; once it
    # is released, python-docx's classes come back.
    retained = type(model.element.body).__name__
    failed = {'error': error(kept[0]), 'state': [template.is_rendered, template.is_saved]}
    kept.clear()
    gc.collect()
    return {'failed': failed, 'retained_body': retained, 'live_body': attempt(lambda: model.paragraphs[0].text),
            'live_header': attempt(lambda: model.sections[0].header.paragraphs[0].text)}


# docx.properties.partial_failure --------------------------------------------

_PROPERTIES = ('title', 'author', 'subject', 'keywords', 'comments', 'category', 'content_status', 'identifier',
               'language', 'version', 'last_modified_by')


@partition('docx.properties.partial_failure', 'all_property_types_and_limits')
def _(engine: Any) -> dict[str, Any]:
    document = body('body')
    for name in _PROPERTIES:
        setattr(document.core_properties, name, '{{ ' + name + ' }}')
    template = load(engine, document)
    context = {name: name.upper() for name in _PROPERTIES} | {'title': 'T' * 255}
    result = outcome(template, lambda: template.render(context))
    long = rendered(engine, document, context | {'subject': 'S' * 256})
    saved = Document(io.BytesIO(blob(template))).core_properties if 'error' not in result else None
    return {'result': result, 'long': long,
            'properties': [getattr(saved, name) for name in _PROPERTIES] if saved is not None else None}


@partition('docx.properties.partial_failure', 'callbacks_and_exception_identity')
def _(engine: Any) -> dict[str, Any]:
    document = body('body {{ value }}')
    document.core_properties.title = '{{ title() }}'
    document.core_properties.subject = '{{ fail() }}'
    raised = ProbeError('property fails')

    def fail() -> str:
        raise raised
    template = load(engine, document)
    model = template.get_docx()
    try:
        template.render({'value': 'V', 'title': lambda: 'titled', 'fail': fail})
        same = None
    except ProbeError as exception:
        same = exception is raised
    return {'same': same, 'title': model.core_properties.title, 'state': [template.is_rendered, template.is_saved]}


@partition('docx.properties.partial_failure', 'multiple_parts_and_repeated_render')
def _(engine: Any) -> dict[str, Any]:
    document = headed('{{ value }}', '{{ value }}')
    document.core_properties.title = 'title {{ value }}'
    document.core_properties.keywords = '{{ value }}, k'
    return {'repeated': rendered(engine, document, {'value': 'one'}, {'value': 'two'})}


@partition('docx.properties.partial_failure', 'metadata_serialization')
def _(engine: Any) -> dict[str, Any]:
    document = body('body')
    document.core_properties.title = '{{ value }}'
    document.core_properties.comments = 'line {{ value }}\nnext'
    return rendered(engine, document, {'value': 'ünï <&> "q" 🙂'})


# docx.body.recovery ---------------------------------------------------------

@partition('docx.body.recovery', 'control_characters_and_unicode')
def _(engine: Any) -> dict[str, Any]:
    return {name: rendered(engine, body('[{{ value }}]'), {'value': value})
            for name, value in (('bell', 'a\x07b'), ('nul', 'a\x00b'), ('unicode', 'façade 日本 🙂 \u200b'),
                                ('surrogate_free', '\ufffd\uffff'))}


@partition('docx.body.recovery', 'malformed_xml_variants')
def _(engine: Any) -> dict[str, Any]:
    return {name: rendered(engine, body('[{{ value }}]'), {'value': value})
            for name, value in (('open_tag', '<b>bold'), ('close_tag', 'x</w:t>y'), ('comment', 'a<!-- c -->b'),
                                ('repr', '<object at 0x1>'), ('cdata', '<![CDATA[x]]>'))}


@partition('docx.body.recovery', 'headers_footers_and_properties')
def _(engine: Any) -> dict[str, Any]:
    document = headed('body {{ value }}', 'head {{ value }}', 'foot {{ value }}')
    document.core_properties.title = 'title {{ value }}'
    return rendered(engine, document, {'value': '<i>x'})


@partition('docx.body.recovery', 'error_identity_and_partial_state')
def _(engine: Any) -> dict[str, Any]:
    template = load(engine, body('{{ value }}'))
    failed = outcome(template, lambda: template.render({'value': '</w:t></w:r></w:p></w:body>'}))
    retried = outcome(template, lambda: template.render({'value': 'fine'}))
    return {'failed': failed, 'retried': retried}


# Probes ---------------------------------------------------------------------

def probe_name(contract: str, name: str) -> str:
    return 'scenario_partition_' + contract.replace('.', '_') + '_' + name


def _execution(scenario: Scenario) -> Callable[[Any], Result]:
    def execute(engine: Any) -> Result:
        try:
            observation = scenario(engine)
        except Exception as exception:  # noqa: BLE001 - the exception is the observation
            observation = {'scenario_error': error(exception)}
        return Result(json.loads(json.dumps(observation, default=str)), {})
    return execute


EXECUTIONS = {probe_name(contract, name): _execution(scenario)
              for contract, partitions in SCENARIOS.items() for name, scenario in partitions.items()}
EXPECTED_PATH = Path(__file__).with_name('scenario_partition_expected.json')
EXPECTED: dict[str, Any] = json.loads(EXPECTED_PATH.read_text()) if EXPECTED_PATH.exists() else {}
PROBES: dict[str, Probe] = {name: Probe(execute, EXPECTED[name], True)
                            for name, execute in EXECUTIONS.items() if name in EXPECTED}
