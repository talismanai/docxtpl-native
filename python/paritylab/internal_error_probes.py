"""Error-occurrence probes for internal pipeline steps.

docxtpl's semi-public render steps (``fix_tables``, ``map_tree``,
``build_xml``...) and the composer steps of ``SubdocComposer`` are internal:
by the owner's decision of 2026-11-16 the package preserves only whether a
call with invalid arguments, or with arguments whose protocols fail, raises
an error, not which error or its message. Each probe calls the step with
every argument replaced and records only whether it raised.

The expected observations in ``internal_error_expected.json`` were recorded
from the reference (docxtpl 0.20.2) by ``scripts/record_reference_observations.py``.
"""
from __future__ import annotations

import importlib
import inspect
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .protocol_base import Probe, Result, template

# Each step's attribute (some composer steps are private, `_name`) and the
# positional parameters the reference's step takes besides `self`; the table
# is checked against docxtpl by tests/test_spec_catalog.py, so the probes load
# without the reference (as the product suite runs them).
STEPS: dict[str, dict[str, tuple[str, int]]] = {
    'template': {
        'render_init': ('render_init', 0), 'xml_to_string': ('xml_to_string', 2), 'get_xml': ('get_xml', 0),
        'write_xml': ('write_xml', 1), 'patch_xml': ('patch_xml', 1), 'render_xml_part': ('render_xml_part', 4),
        'render_properties': ('render_properties', 2), 'render_footnotes': ('render_footnotes', 2),
        'resolve_listing': ('resolve_listing', 1), 'build_xml': ('build_xml', 2), 'map_tree': ('map_tree', 1),
        'get_headers_footers': ('get_headers_footers', 1), 'get_part_xml': ('get_part_xml', 1),
        'get_headers_footers_encoding': ('get_headers_footers_encoding', 1),
        'build_headers_footers_xml': ('build_headers_footers_xml', 3),
        'map_headers_footers_xml': ('map_headers_footers_xml', 2), 'fix_tables': ('fix_tables', 1),
        'fix_docpr_ids': ('fix_docpr_ids', 1), 'post_processing': ('post_processing', 1),
        'pre_processing': ('pre_processing', 0), 'get_pic_map': ('get_pic_map', 0),
    },
    'composer': {
        'attach_parts': ('attach_parts', 2), 'add_diagrams': ('add_diagrams', 2),
        'restart_first_numbering': ('restart_first_numbering', 2), 'add_footnotes': ('add_footnotes', 2),
        'footnote_part': ('footnote_part', 0), 'add_styles': ('add_styles', 2),
        'add_numberings': ('add_numberings', 2), 'next_numbering_ids': ('_next_numbering_ids', 0),
        'numbering_part': ('numbering_part', 0), 'insert_num': ('_insert_num', 1),
        'insert_abstract_num': ('_insert_abstract_num', 1), 'replace_mapped_num_id': ('_replace_mapped_num_id', 2),
        'add_styles_from_other_parts': ('add_styles_from_other_parts', 1), 'add_images': ('add_images', 2),
        'add_shapes': ('add_shapes', 2), 'create_style_id_mapping': ('_create_style_id_mapping', 1),
        'mapped_style_id': ('mapped_style_id', 1), 'add_referenced_parts': ('add_referenced_parts', 3),
        'add_relationship': ('add_relationship', 3), 'renumber_docpr_ids': ('renumber_docpr_ids', 0),
        'renumber_nvpicpr_ids': ('renumber_nvpicpr_ids', 0), 'renumber_bookmarks': ('renumber_bookmarks', 0),
        'remove_header_and_footer_references': ('remove_header_and_footer_references', 2),
        'fix_section_types': ('fix_section_types', 1),
    },
}


class Opaque:
    """An argument of no expected type."""
    __slots__ = ()


class Failing:
    """An argument whose protocols fail."""
    def _fail(self, *args: Any, **kwargs: Any) -> Any:
        raise ValueError('probe argument failure')
    __str__ = __iter__ = __len__ = __getitem__ = __getattr__ = __bool__ = __index__ = __fspath__ = _fail


def reference_step(cls: type, name: str) -> tuple[str, int]:
    """The step's attribute on the reference class and its positional parameters."""
    attribute = name if hasattr(cls, name) else '_' + name
    signature = inspect.signature(getattr(cls, attribute))
    return attribute, sum(1 for parameter in list(signature.parameters.values())[1:]
                          if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD))


def _target(engine: Any, owner: str) -> Any:
    if owner == 'template':
        document = engine.DocxTemplate(io.BytesIO(template('{{ value }}', header='{{ value }}')))
        document.render_init()
        return document
    from docx import Document
    return importlib.import_module(engine.__name__ + '.subdoc').SubdocComposer(Document())


def _probe(owner: str, name: str, argument: Callable[[], Any]) -> Callable[[Any], Result]:
    attribute, count = STEPS[owner][name]

    def execute(engine: Any) -> Result:
        method = getattr(_target(engine, owner), attribute)
        arguments = [argument() for _ in range(max(count, 1))]
        try:
            method(*arguments)
        except Exception:  # noqa: BLE001 - only the occurrence of an error is preserved
            return Result({'raises': True}, {})
        return Result({'raises': False}, {})
    return execute


def _scenarios() -> dict[str, Callable[[Any], Result]]:
    table = {}
    for owner, steps in STEPS.items():
        for name in steps:
            table[f'internal_{owner}_{name}_invalid_input'] = _probe(owner, name, Opaque)
            table[f'internal_{owner}_{name}_exceptions'] = _probe(owner, name, Failing)
    return table


EXECUTIONS = _scenarios()
EXPECTED_PATH = Path(__file__).with_name('internal_error_expected.json')
EXPECTED: dict[str, Any] = json.loads(EXPECTED_PATH.read_text()) if EXPECTED_PATH.exists() else {}
PROBES: dict[str, Probe] = {name: Probe(execute, EXPECTED[name], True)
                            for name, execute in EXECUTIONS.items() if name in EXPECTED}
