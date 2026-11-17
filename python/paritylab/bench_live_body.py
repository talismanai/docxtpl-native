"""Compare body operations on an in-memory python-docx document."""
import argparse
import json
import os
import platform
import statistics
import time

from docx import Document as NewDocument
from lxml import etree
import docxtpl_native


class Document:
    def __init__(self):
        self.docx = NewDocument()
        self.docx.add_paragraph('Rendered body content')

    def xml_to_string(self, node):
        return etree.tostring(node, encoding='unicode')


def previous(document, render_xml):
    source = document.xml_to_string(document.docx._element.body)
    body = render_xml(source, document.docx._part)
    tree = etree.fromstring(body, parser=etree.XMLParser(recover=True))
    document.docx._element.replace(document.docx._element.body, tree)


def measure(iterations):
    document = Document()
    extension = docxtpl_native._extension()
    def render(source, part): return source
    def old(): previous(document, render)
    def current():
        body = extension.render_live_body(document, render)
        extension.commit_live_body(document, body)
    for _ in range(100): old(); current()
    samples = []
    for index in range(9):
        pair = {}
        for name in (('previous', 'native') if index % 2 == 0 else ('native', 'previous')):
            function = old if name == 'previous' else current
            start = time.perf_counter_ns()
            for _ in range(iterations): function()
            pair[name] = (time.perf_counter_ns()-start)/iterations
        samples.append(pair)
    assert document.docx.paragraphs[0].text == 'Rendered body content'
    return {'source_revision': os.environ.get('GITHUB_SHA'), 'python': platform.python_version(),
            'iterations': iterations, 'warmups': 100, 'samples': samples,
            'median_previous_over_native': statistics.median(s['previous']/s['native'] for s in samples),
            'extension': extension.__file__,
            'scope': 'In-memory python-docx body operations with an identity callback; no template compilation or DOCX IO.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--iterations', type=int, default=3000)
    args = parser.parse_args()
    if args.iterations <= 0: parser.error('iterations must be positive')
    print(json.dumps(measure(args.iterations), indent=2))
