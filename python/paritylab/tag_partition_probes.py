"""Differential probes for the partitions of docxtpl's structural Word tags.

The paragraph (`{%p %}`), run (`{%r %}`), `cellbg`, `colspan`, horizontal
merge (`hm`) and vertical merge (`vm`) tags share the catalog partitions
empty, nested, invalid placement, malformed tag, styles interactions and
repeated render. Each probe builds a document, renders it and records the
document's text, paragraph styles, bold runs, spans, fills and merges, or the
exception.

Expected observations in ``tag_partition_expected.json`` were recorded from
the reference (docxtpl 0.20.2) by ``scripts/record_reference_observations.py``.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from docx import Document
from lxml import etree

from .protocol_base import Probe, Result

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
NS = {'w': W}
Build = Callable[[], tuple[Any, dict[str, Any]]]


def _observe(blob: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(blob)) as package:
        root = etree.fromstring(package.read('word/document.xml'))
    document = Document(io.BytesIO(blob))
    return {
        'paragraphs': [[paragraph.text, paragraph.style.name] for paragraph in document.paragraphs],
        'bold': [run.text for paragraph in document.paragraphs for run in paragraph.runs if run.bold],
        'cells': root.xpath('//w:tc//w:t/text()', namespaces=NS),
        'spans': root.xpath('//w:gridSpan/@w:val', namespaces=NS),
        'fills': root.xpath('//w:shd/@w:fill', namespaces=NS),
        'merges': root.xpath('//w:vMerge/@w:val', namespaces=NS),
    }


def _render(engine: Any, source: Any, contexts: list[dict[str, Any]]) -> dict[str, Any]:
    buffer = io.BytesIO()
    source.save(buffer)
    document = engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
    try:
        for context in contexts:
            document.render(context)
        output = io.BytesIO()
        document.save(output)
    except Exception as error:  # noqa: BLE001 - the exception is the observation
        return {'type': type(error).__module__ + '.' + type(error).__qualname__,
                'message': re.sub(r' at 0x[0-9a-fA-F]+', '', str(error))}
    return _observe(output.getvalue())


def _paragraphs(*texts: str, style: str | None = None) -> Any:
    source = Document()
    for text in texts:
        source.add_paragraph(text, style=style if text and not text.startswith('{%') else None)
    return source


def _runs(*runs: tuple[str, bool]) -> Any:
    source = Document()
    paragraph = source.add_paragraph()
    for text, bold in runs:
        paragraph.add_run(text).bold = bold
    return source


def _cell_table(*rows: list[str]) -> Any:
    source = Document()
    table = source.add_table(rows=len(rows), cols=max(len(row) for row in rows))
    for index, row in enumerate(rows):
        for column, text in enumerate(row):
            table.cell(index, column).text = text
    return source


def _shaded(text: str, fill: str) -> Any:
    source = _cell_table([text, 'Y'])
    cell = source.tables[0].cell(0, 0)._tc
    properties = cell.get_or_add_tcPr()
    properties.append(etree.fromstring(f'<w:shd xmlns:w="{W}" w:val="clear" w:fill="{fill}"/>'))
    return source


SHOW = {'show': True, 'inner': True}
ITEMS = {'items': [{'group': 'g', 'name': 'A'}, {'group': 'g', 'name': 'B'}, {'group': 'h', 'name': 'C'}]}
TAGS: dict[str, dict[str, tuple[Build, list[dict[str, Any]]]]] = {
    'paragraph': {
        'empty': (lambda: _paragraphs('{%p if show %}', '{%p endif %}', 'after'), [SHOW]),
        'nested': (lambda: _paragraphs('{%p if show %}', 'a', '{%p if inner %}', 'b', '{%p endif %}',
                                       '{%p endif %}', 'after'), [SHOW]),
        'invalid_placement': (lambda: _paragraphs('before {%p if show %} inside', '{%p endif %}', 'after'), [SHOW]),
        'malformed_tag': (lambda: _paragraphs('{%p if show %}', 'inside', 'after'), [SHOW]),
        'styles_interactions': (lambda: _paragraphs('{%p if show %}', 'heading', '{%p endif %}', style='Heading 1'),
                                [SHOW]),
        'repeated_render': (lambda: _paragraphs('{%p if show %}', 'inside', '{%p endif %}', 'after'),
                            [SHOW, {'show': False}]),
    },
    'run': {
        'empty': (lambda: _runs(('{%r if show %}', False), ('{%r endif %}', False), ('after', False)), [SHOW]),
        'nested': (lambda: _runs(('{%r if show %}', False), ('a', True), ('{%r if inner %}', False), ('b', False),
                                 ('{%r endif %}', False), ('{%r endif %}', False)), [SHOW]),
        'invalid_placement': (lambda: _paragraphs('{%r if show %}', 'inside', '{%r endif %}'), [SHOW]),
        'malformed_tag': (lambda: _runs(('{%r if show', False), ('inside', False)), [SHOW]),
        'styles_interactions': (lambda: _runs(('{%r if show %}', True), ('bold', True), ('{%r endif %}', True),
                                              ('plain', False)), [SHOW]),
        'repeated_render': (lambda: _runs(('{%r if show %}', False), ('inside', True), ('{%r endif %}', False)),
                            [SHOW, {'show': False}]),
    },
    'cellbg': {
        'empty': (lambda: _cell_table(['{% cellbg value %}X', 'Y']), [{'value': ''}]),
        'nested': (lambda: _cell_table(['{%tr for item in items %}'], ['{% cellbg item.color %}{{ item.name }}'],
                                       ['{%tr endfor %}']),
                   [{'items': [{'color': 'FF0000', 'name': 'A'}, {'color': '00FF00', 'name': 'B'}]}]),
        'invalid_placement': (lambda: _paragraphs('{% cellbg value %}X'), [{'value': 'FF0000'}]),
        'malformed_tag': (lambda: _cell_table(['{% cellbg %}X', 'Y']), [{'value': 'FF0000'}]),
        'styles_interactions': (lambda: _shaded('{% cellbg value %}X', '0000FF'), [{'value': 'FF0000'}]),
        'repeated_render': (lambda: _cell_table(['{% cellbg value %}X', 'Y']),
                            [{'value': 'FF0000'}, {'value': '00FF00'}]),
    },
    'colspan': {
        'empty': (lambda: _cell_table(['{% colspan value %}X', 'Y']), [{'value': 0}]),
        'nested': (lambda: _cell_table(['{%tr for item in items %}'], ['{% colspan item.span %}{{ item.name }}', 'Y'],
                                       ['{%tr endfor %}']),
                   [{'items': [{'span': 2, 'name': 'A'}, {'span': 1, 'name': 'B'}]}]),
        'invalid_placement': (lambda: _paragraphs('{% colspan value %}X'), [{'value': 2}]),
        'malformed_tag': (lambda: _cell_table(['{% colspan %}X', 'Y']), [{'value': 2}]),
        'styles_interactions': (lambda: _shaded('{% colspan value %}X', '0000FF'), [{'value': 2}]),
        'repeated_render': (lambda: _cell_table(['{% colspan value %}X', 'Y']), [{'value': 2}, {'value': 1}]),
    },
    'horizontal_merge': {
        'empty': (lambda: _cell_table(['{%tc for item in items %}', '{% hm %}{{ item }}', '{%tc endfor %}']),
                  [{'items': []}]),
        'nested': (lambda: _cell_table(['{%tc for item in items %}', '{% hm %}{{ item }}', '{%tc endfor %}'],
                                       ['{%tc for item in items %}', '{% hm %}{{ item }}', '{%tc endfor %}']),
                   [{'items': ['A', 'B']}]),
        'invalid_placement': (lambda: _paragraphs('{% hm %}X'), [{}]),
        'malformed_tag': (lambda: _cell_table(['{%tc for item in items %}', '{% hm {{ item }}', '{%tc endfor %}']),
                          [{'items': ['A', 'B']}]),
        'styles_interactions': (lambda: _shaded('{% hm %}X', '0000FF'), [{}]),
        'repeated_render': (lambda: _cell_table(['{%tc for item in items %}', '{% hm %}{{ item }}', '{%tc endfor %}']),
                            [{'items': ['A', 'B']}, {'items': ['C']}]),
    },
    'vertical_merge': {
        'empty': (lambda: _cell_table(['{%tr for item in items %}', ''], ['{% vm %}{{ item.group }}', '{{ item.name }}'],
                                      ['{%tr endfor %}', '']), [{'items': []}]),
        'nested': (lambda: _cell_table(['{%tr for item in items %}', ''], ['{% vm %}{{ item.group }}', '{{ item.name }}'],
                                       ['{%tr endfor %}', '']), [ITEMS]),
        'invalid_placement': (lambda: _paragraphs('{% vm %}X'), [{}]),
        'malformed_tag': (lambda: _cell_table(['{%tr for item in items %}', ''], ['{% vm {{ item.group }}', ''],
                                              ['{%tr endfor %}', '']), [ITEMS]),
        'styles_interactions': (lambda: _shaded('{% vm %}X', '0000FF'), [{}]),
        'repeated_render': (lambda: _cell_table(['{%tr for item in items %}', ''],
                                                ['{% vm %}{{ item.group }}', '{{ item.name }}'],
                                                ['{%tr endfor %}', '']), [ITEMS, {'items': ITEMS['items'][:1]}]),
    },
}


def _probe(build: Build, contexts: list[dict[str, Any]]) -> Callable[[Any], Result]:
    def execute(engine: Any) -> Result:
        return Result(_render(engine, build(), contexts), {})
    return execute


EXECUTIONS = {f'tag_partition_{tag}_{partition}': _probe(build, contexts)
             for tag, partitions in TAGS.items() for partition, (build, contexts) in partitions.items()}
EXPECTED_PATH = Path(__file__).with_name('tag_partition_expected.json')
EXPECTED: dict[str, Any] = json.loads(EXPECTED_PATH.read_text()) if EXPECTED_PATH.exists() else {}
PROBES: dict[str, Probe] = {name: Probe(execute, EXPECTED[name], True)
                            for name, execute in EXECUTIONS.items() if name in EXPECTED}
