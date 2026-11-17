"""The serializer must preserve object protocols and avoid runtime imports."""
import builtins
import sys
import subprocess
import unittest
import docxtpl
import docxtpl_native
import docx.opc.oxml
from lxml import etree

class NativeXmlSerializationTests(unittest.TestCase):
    def test_direct_c_subtrees_namespaces_unicode_and_tail(self):
        sources = [
            b'<r xmlns="urn:r" xmlns:a="urn:a" xmlns:unused="urn:u"><a:x a:n="1">abc<y/>tail</a:x>after</r>',
            b'<r xmlns:a="urn:outer"><x xmlns:a="urn:inner"><a:y/></x></r>',
            b'<r><x a="&quot;&amp;&#13;&#10;&#9;">a &amp; b<![CDATA[<c>]]></x>tail</r>',
            '<r><x>façade Ελληνικά 😀</x>tail</r>'.encode(),
            b'<r><!--c--><?pi value?><x/></r>',
        ]
        for source in sources:
            root = etree.fromstring(source)
            for node in root.iter():
                with self.subTest(source=source, tag=node.tag):
                    self.assertEqual(docxtpl_native.DocxTemplate(None).xml_to_string(node),
                                     docxtpl.DocxTemplate(None).xml_to_string(node))

    def test_direct_c_tree_contexts_doctype_and_empty_tree(self):
        for source in [b'<root/>', b'<?before x?><root/><!--after-->',
                       b'<?before x?><!DOCTYPE root [<!ENTITY e "v">]><root>&e;</root><?after y?>',
                       b'<!DOCTYPE root SYSTEM "example.dtd"><root><child/></root>']:
            root = etree.fromstring(source, etree.XMLParser(resolve_entities=False, no_network=True))
            for value in [root.getroottree(), etree.ElementTree(root), etree.ElementTree(root[0]) if len(root) else root]:
                with self.subTest(source=source, value=type(value).__name__):
                    self.assertEqual(docxtpl_native.DocxTemplate(None).xml_to_string(value),
                                     docxtpl.DocxTemplate(None).xml_to_string(value))
        for value in [etree.ElementTree(), etree.ElementTree(etree.Comment('comment'))]:
            self.assertEqual(docxtpl_native.DocxTemplate(None).xml_to_string(value),
                             docxtpl.DocxTemplate(None).xml_to_string(value))

    def test_direct_c_serialization_preserves_graph_and_references(self):
        root = etree.fromstring(b'<r xmlns:a="urn:a"><a:x/><y/></r>')
        child = root[0]
        template = docxtpl_native.DocxTemplate(None)
        template.xml_to_string(child)
        before = (etree.tostring(root), sys.getrefcount(root), sys.getrefcount(child))
        for _ in range(200): template.xml_to_string(child)
        self.assertEqual((etree.tostring(root), sys.getrefcount(root), sys.getrefcount(child)), before)
        self.assertIs(child.getparent(), root)

    def test_serializer_override_remains_a_binding_callback(self):
        docxtpl_native._extension()
        original = etree.tostring
        calls = []
        def custom(*args, **kwargs):
            calls.append((args, kwargs))
            return 'custom'
        node = etree.Element('r')
        try:
            etree.tostring = custom
            self.assertEqual(docxtpl_native.DocxTemplate(None).xml_to_string(node), 'custom')
            self.assertEqual(calls, [((node,), {'encoding': 'unicode', 'pretty_print': False})])
        finally:
            etree.tostring = original

    def test_override_before_extension_initialization(self):
        code = '''
import docxtpl_native
from lxml import etree
original = etree.tostring
try:
    etree.tostring = lambda *args, **kwargs: 'before-init'
    assert docxtpl_native.DocxTemplate(None).xml_to_string(etree.Element('r')) == 'before-init'
finally:
    etree.tostring = original
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_exported_c_types_ignore_mutated_python_type_aliases(self):
        node = etree.Element('r')
        original = etree._Element
        try:
            etree._Element = object
            self.assertEqual(docxtpl_native.DocxTemplate(None).xml_to_string(node), '<r/>')
        finally:
            etree._Element = original

    @unittest.skipUnless(sys.platform != 'win32', 'native provider lookup uses dladdr on Unix')
    def test_native_library_is_found_from_its_c_api_not_python_filename(self):
        code = '''
import docxtpl_native
from lxml import etree
etree.__file__ = '/not/the/native/library.so'
assert docxtpl_native.DocxTemplate(None).xml_to_string(etree.Element('r')) == '<r/>'
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_opc_parser_module_rebinding_does_not_change_serializer(self):
        tree = etree.fromstring(b'<root/>')
        docxtpl_native._extension()
        original = docx.opc.oxml.etree
        try:
            docx.opc.oxml.etree = object()
            self.assertEqual(docxtpl_native.DocxTemplate(None).xml_to_string(tree),
                             docxtpl.DocxTemplate(None).xml_to_string(tree))
        finally:
            docx.opc.oxml.etree = original

    def test_does_not_import_at_call_time(self):
        tree = etree.fromstring(b'<root>text</root>')
        docxtpl_native._extension()
        def observe(engine):
            original = builtins.__import__
            def guarded(name, *args, **kwargs):
                if name == 'lxml': raise RuntimeError('unexpected runtime import')
                return original(name, *args, **kwargs)
            try:
                builtins.__import__ = guarded
                return engine.DocxTemplate(None).xml_to_string(tree)
            finally:
                builtins.__import__ = original
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_ignores_encoding_argument_and_preserves_errors(self):
        tree = etree.fromstring(b'<root>text</root>')
        for value in (None, 'ascii', object()):
            self.assertEqual(docxtpl_native.DocxTemplate(None).xml_to_string(tree, value),
                             docxtpl.DocxTemplate(None).xml_to_string(tree, value))
        def error(engine):
            try: engine.DocxTemplate(None).xml_to_string(object())
            except Exception as exc: return type(exc).__name__, str(exc)
            raise AssertionError('expected exception')
        self.assertEqual(error(docxtpl_native), error(docxtpl))
