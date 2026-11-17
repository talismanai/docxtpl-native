"""Body rendering and replacement preserve observable object operations."""
import unittest
from unittest.mock import patch

import docxtpl_native


def previous_render(document, render_xml):
    source = document.xml_to_string(document.docx._element.body)
    return render_xml(source, document.docx._part)


def previous_commit(document, body):
    tree = etree.fromstring(body, parser=etree.XMLParser(recover=True))
    document.docx._element.replace(document.docx._element.body, tree)


class LiveBodyTests(unittest.TestCase):
    def observe(self, native, fail=None, swap=False):
        events = []
        failure = RuntimeError('original failure')

        def mark(name):
            events.append(name)
            if fail == name:
                raise failure

        class Element:
            value = 'old body'
            @property
            def body(self):
                mark('body'); return self.value
            @property
            def replace(self):
                mark('lookup replace')
                def replace(old, tree):
                    mark(('replace', old, tree)); self.value = tree
                return replace

        element = Element()
        class Model:
            @property
            def _element(self):
                mark('element'); return element
            @property
            def _part(self):
                mark('part'); return 'part value'
        class Document:
            @property
            def docx(self):
                mark('docx'); return Model()
            @property
            def xml_to_string(self):
                mark('lookup serialize')
                def serialize(body):
                    mark(('serialize', body)); return 'source'
                return serialize
        class Parser:
            def __init__(self, label): self.label = label
            def __getattribute__(self, name):
                if name == 'fromstring':
                    mark('lookup fromstring')
                    if swap: globals()['etree'] = Parser('alternate')
                if name == 'XMLParser': mark(('lookup parser', object.__getattribute__(self, 'label')))
                return object.__getattribute__(self, name)
            def XMLParser(self, recover):
                mark(('parser', self.label, recover)); return 'parser:'+self.label
            def fromstring(self, body, parser):
                mark(('fromstring', body, parser)); return 'new tree'
        def render(source, part):
            mark(('render', source, part)); return 'rendered'

        extension = docxtpl_native._extension()
        with patch.dict(globals(), {'etree': Parser('original')}):
            try:
                body = extension.render_live_body(Document(), render) if native else previous_render(Document(), render)
                if native: extension.commit_live_body(Document(), body)
                else: previous_commit(Document(), body)
                outcome = ('ok',)
            except Exception as error:
                outcome = ('error', type(error), str(error), error is failure)
        return outcome, events, element.value

    def test_order_dynamic_globals_and_partial_failures(self):
        for options in ({}, {'swap': True}, {'fail': 'part'}, {'fail': ('lookup parser', 'original')},
                        {'fail': ('fromstring', 'rendered', 'parser:original')},
                        {'fail': ('replace', 'old body', 'new tree')}):
            with self.subTest(**options):
                self.assertEqual(self.observe(True, **options), self.observe(False, **options))


if __name__ == '__main__': unittest.main()
