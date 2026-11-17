"""Differential coverage of the direct encoding API, including malformed XML."""
import unittest
import random
import array
import docxtpl
import docxtpl_native


class HeaderEncodingTests(unittest.TestCase):
    def test_custom_buffer_protocol_has_reference_side_effects(self):
        def observe(engine):
            trace = []
            class Buffer:
                def __buffer__(self, flags):
                    trace.append(('acquire', flags))
                    return memoryview(b'abc')
                def __release_buffer__(self, view):
                    trace.append(('release', view.tobytes()))
            try:
                engine.DocxTemplate(None).get_headers_footers_encoding(Buffer())
            except TypeError as error:
                return str(error), trace
            raise AssertionError('expected TypeError')
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_invalid_types_match_reference_errors(self):
        reference = docxtpl.DocxTemplate(None)
        candidate = docxtpl_native.DocxTemplate(None)
        for value in (1, None, b'abc', bytearray(b'abc'), memoryview(b'abc'),
                      memoryview(b'abcd')[::2], array.array('i', [1]), [], object()):
            with self.subTest(type=type(value).__name__, value=repr(value)):
                with self.assertRaises(TypeError) as expected:
                    reference.get_headers_footers_encoding(value)
                with self.assertRaises(TypeError) as actual:
                    candidate.get_headers_footers_encoding(value)
                self.assertEqual(str(actual.exception), str(expected.exception))

    def test_unpaired_surrogates_keep_original_value_and_boundaries(self):
        reference = docxtpl.DocxTemplate(None)
        candidate = docxtpl_native.DocxTemplate(None)
        class StringSubclass(str):
            def encode(self, *args, **kwargs):
                raise AssertionError('overridden encode must not run')
            def __getitem__(self, item):
                raise AssertionError('overridden slicing must not run')
        for source in [
            '<?xml encoding="x\ud800y"?>',
            '<?xml encoding="\udc00"?>',
            '<?xml \ud800encoding="UTF-8"?>',
            '<?xml 😀 encoding="😀\ud800\ufffdz"?>',
            '<?xml encoding="\ud800" encoding="two\udc00"?>',
            '<?xml encoding="utf-8" \ud800?>',
            '<?xml encoding="a\x00\ud800z"?>',
            '\ud800<?xml encoding="x"?>',
            StringSubclass('<?xml encoding="\ud800"?>'),
        ]:
            with self.subTest(source=ascii(source)):
                self.assertEqual(candidate.get_headers_footers_encoding(source),
                                 reference.get_headers_footers_encoding(source))

    def test_picture_map_is_the_live_binding_object(self):
        import io
        from paritylab.protocol_base import template
        for engine in (docxtpl, docxtpl_native):
            with self.subTest(engine=engine.__name__):
                tpl = engine.DocxTemplate(io.BytesIO(template('X')))
                tpl.render_init()
                mapping = tpl.get_pic_map()
                self.assertIs(mapping, tpl.pic_map)
                mapping['x'] = 'y'
                self.assertEqual(tpl.get_pic_map()['x'], 'y')

    def test_matches_reference_declaration_scan(self):
        reference = docxtpl.DocxTemplate(None)
        candidate = docxtpl_native.DocxTemplate(None)
        cases = [
            ('<?xml version="1.0" encoding="ISO-8859-1"?>', 'ISO-8859-1'),
            ('<?XML ENCODING="UTF-16"?>', 'UTF-16'),
            ('<?xml encoding="first" encoding="second"?>', 'second'),
            ('<?xml encoding="first" encoding=""?>', 'first'),
            ('<?xml encoding="a?b"?>', 'a?b'),
            ('<?xml éencoding="x"?>', 'utf-8'),
            ('<?xml ·encodİng="x"?>', 'x'),
            ('<?xml \u0345encoding="x"?>', 'x'),
            ('<?xml \u05b0encoding="x"?>', 'x'),
            ('<?xml \u08d4encoding="x"?>', 'x'),
            ('<?xml encoding ="x"?>', 'utf-8'),
            ("<?xml encoding='x'?>", 'utf-8'),
            (' <?xml encoding="x"?>', 'utf-8'),
            ('<?xml? encoding="x"?>', 'utf-8'),
            ('<?xml encoding=""?>', 'utf-8'),
            ('<root/>', 'utf-8'),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(reference.get_headers_footers_encoding(value), expected)
                self.assertEqual(candidate.get_headers_footers_encoding(value), expected)

    def test_malformed_declarations_match_reference(self):
        rng = random.Random(512)
        reference = docxtpl.DocxTemplate(None)
        candidate = docxtpl_native.DocxTemplate(None)
        fragments = ['encoding="x"', 'encoding=""', 'ENCODING="utf-8"',
                     'encoding="a?b"', 'é', '\u0301', '\u0345', '\u05b0', '_', '?', ' ', '"', '']
        for _ in range(1000):
            source = '<?xml' + ''.join(rng.choices(fragments, k=rng.randrange(8)))
            self.assertEqual(candidate.get_headers_footers_encoding(source),
                             reference.get_headers_footers_encoding(source), repr(source))

    def test_every_unicode_code_point_matches_reference_word_boundary(self):
        reference = docxtpl.DocxTemplate(None)
        candidate = docxtpl_native.DocxTemplate(None)
        for code in range(0x110000):
            source = '<?xml ' + chr(code) + 'encoding="x"?>'
            expected = reference.get_headers_footers_encoding(source)
            actual = candidate.get_headers_footers_encoding(source)
            if actual != expected:
                self.fail(f'U+{code:04X}: expected {ascii(expected)}, got {ascii(actual)}')
