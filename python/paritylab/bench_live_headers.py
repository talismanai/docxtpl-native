"""Compare live header traversal against its former Python implementation."""
import argparse
import json
import os
import platform
import statistics
import time

import docxtpl_native


class Part:
    def __init__(self, blob=b'header', rels=None):
        self.blob = blob
        self.rels = rels or {}
        self.partname = '/word/header.xml'
        self.content_type = 'application/xml'
        self.package = None

    def load_rel(self, kind, target, rid, external):
        self.rels[rid] = InnerRelation(kind, target, rid, external)


class InnerRelation:
    def __init__(self, kind, target, rid, external):
        self.reltype, self._target, self.rId, self.is_external = kind, target, rid, external


class Relation:
    def __init__(self, kind, target):
        self.reltype, self._target = kind, target

    @property
    def target_part(self):
        return self._target


class XmlPart:
    @staticmethod
    def load(name, content, xml, package):
        return Part(xml)


def parse_xml(blob):
    return blob.decode('utf-8')


class Document:
    HEADER_URI = 'header'
    FOOTER_URI = 'footer'

    def __init__(self, relations):
        self.docx = type('Model', (), {'_part': type('MainPart', (), {'rels': relations})()})()

    def xml_to_string(self, value):
        return value


def previous(document, render_xml):
    for uri in (document.HEADER_URI, document.FOOTER_URI):
        for relationship in document.docx._part.rels.values():
            if relationship.reltype != uri or not relationship.target_part.blob:
                continue
            old = relationship.target_part
            source = document.xml_to_string(parse_xml(old.blob))
            xml = render_xml(source, old).encode('utf-8')
            new = XmlPart.load(old.partname, old.content_type, xml, old.package)
            for rel in old.rels.values():
                new.load_rel(rel.reltype, rel._target, rel.rId, rel.is_external)
            relationship._target = new


def measure(case, iterations):
    relations = {'other': Relation('other', Part())}
    if case == 'header_footer':
        for kind in ('header', 'footer'):
            inner = InnerRelation('image', 'image-target', 'rId1', False)
            relations[kind] = Relation(kind, Part(rels={'rId1': inner}))
    document = Document(relations)
    def render(source, part):
        return source
    native = docxtpl_native._extension().render_live_headers
    def old():
        previous(document, render)
    def current():
        native(document, render, 'utf-8')
    for _ in range(1000):
        old(); current()
    samples = []
    for index in range(9):
        pair = {}
        for name in (('previous', 'native') if index % 2 == 0 else ('native', 'previous')):
            function = old if name == 'previous' else current
            start = time.perf_counter_ns()
            for _ in range(iterations):
                function()
            pair[name] = (time.perf_counter_ns() - start) / iterations
        samples.append(pair)
    for name in ('header', 'footer') if case == 'header_footer' else ():
        part = relations[name]._target
        assert part.blob == b'header' and part.rels['rId1']._target == 'image-target'
    return {'source_revision': os.environ.get('GITHUB_SHA'), 'python': platform.python_version(),
            'case': case, 'iterations': iterations, 'warmups': 1000, 'samples': samples,
            'median_previous_over_native': statistics.median(s['previous'] / s['native'] for s in samples),
            'extension': docxtpl_native._extension().__file__,
            'scope': 'Live header/footer traversal using stub objects; no template compilation or DOCX IO.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=('none', 'header_footer'), required=True)
    parser.add_argument('--iterations', type=int, default=10000)
    args = parser.parse_args()
    if args.iterations <= 0:
        parser.error('iterations must be positive')
    print(json.dumps(measure(args.case, args.iterations), indent=2))
