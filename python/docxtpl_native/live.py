"""Phase commits for an exposed original python-docx/lxml object graph.

Template execution is always the native VM. The Python owner keeps actual API
objects alive and commits body/header/property replacements in reference order.
This path is selected for exposed models, Python objects/callbacks and supplied
environments. Native file composition still needs its graph integration adapter.
"""
# The native bridges resolve these globals at the same point as the original
# Python phases, including replacements installed by callbacks.
from docx.opc.oxml import parse_xml
from docx.opc.part import XmlPart
from lxml import etree  # ty: ignore[import-not-found]

from . import _extension

_NATIVE_GLOBALS = (parse_xml, XmlPart, etree)


def render(document, context, configuration):
    configuration.package_values_live=True
    session=_extension().PythonRenderSession(configuration,context)

    def render_xml(source, part):
        return _extension().render_live_xml(document,session,source,part)

    body=_extension().render_live_body(document,render_xml)
    configuration.end_object_sources()
    body,document.docx_ids_index=_extension().finish_python_body(body,document.docx_ids_index)
    # As docxtpl's render keeps `tree`: held by this frame, the committed
    # body keeps its parsed element while a later phase's traceback lives.
    tree=_extension().commit_live_body(document,body)

    _extension().render_live_headers(document,render_xml,'utf-8')

    _extension().render_live_properties(document,session,'<string>')

    _extension().render_live_footnotes(document,render_xml,
        'application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml','utf-8')

    document.last_execution=session.statistics()
    document._rendered=document._rendered_replacements=None
    document.is_rendered=True
    del tree
