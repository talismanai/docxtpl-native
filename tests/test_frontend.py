import io
import unittest
from docxtpl_native import DocxTemplate,UnsupportedFeature,_value,capabilities

class FrontendTests(unittest.TestCase):
    def test_constructor_defers_loading(self):DocxTemplate('/missing/template.docx')
    def test_int_overflow_is_explicit(self):
        with self.assertRaises(UnsupportedFeature):_value(10**100)
    def test_callbacks_are_not_silently_stringified(self):
        with self.assertRaises(UnsupportedFeature):_value(lambda:1)
    def test_cycles_are_not_silently_truncated(self):
        x=[];x.append(x)
        with self.assertRaises(UnsupportedFeature):_value(x)
    def test_shared_acyclic_lists_allowed(self):
        x=[1];self.assertEqual(_value([x,x])['kind'],'list')
    def test_nonfinite_floats_rejected(self):
        with self.assertRaises(UnsupportedFeature):_value(float('nan'))
    def test_tag_named_user_dictionary_cannot_become_rich_xml(self):
        value=_value({'kind':'xml','value':'<x/>'});self.assertEqual(value['kind'],'map')
    def test_no_fallback_capability(self):self.assertFalse(capabilities()['reference_fallback'])


    def test_simple_source_matches_python_recognizer(self):
        import keyword
        import re
        from docxtpl_native.simple_frontend import accepts
        # A bare name, or a name through one argument-free standard filter
        # (parsed by the AST check, where a leading `{` is a dict literal).
        pattern = re.compile(r'(?<!\{)\{\{[ \t\r\n]*([A-Za-z_][A-Za-z0-9_]*)[ \t\r\n]*'
                             r'\|[ \t\r\n]*(?:upper|lower|title|trim|length)[ \t\r\n]*\}\}'
                             r'|\{\{[ \t\r\n]*([A-Za-z_][A-Za-z0-9_]*)[ \t\r\n]*\}\}')
        reserved = {'self', 'super', 'loop', 'caller'}
        # Literal names print their value, whether or not the context has them.
        literals = {'true', 'false', 'none', 'True', 'False', 'None'}
        def reference(source, names):
            for match in pattern.finditer(source):
                name = match[1] or match[2]
                if name in literals:
                    # Parsed by the AST check, where a leading `{` is a dict literal.
                    if source[:match.start()].endswith('{'):
                        return False
                    continue
                if name not in names or name in reserved or keyword.iskeyword(name):
                    return False
            return not any(marker in pattern.sub('', source) for marker in ('{{', '{%', '{#'))
        names = frozenset({'x', '_a2', 'True', 'if', 'loop'})
        pieces = ['', 'texto é ', '{', '}', '{{x}}', '{{ _a2\n}}', '{{missing}}',
                  '{{if}}', '{{True}}', '{{loop}}', '{{ x | upper }}', '{{', '{%', '{#',
                  '{{é}}', '{{{{x}}', '{{x}}}', '{ {{x}}', '{{x}}{', '{{x}}%', '{{x}}#']
        for left in pieces:
            for right in pieces:
                source = left + right
                with self.subTest(source=source):
                    self.assertEqual(accepts(source, names), reference(source, names))

    def test_listing_matches_reference(self):
        from docxtpl import Listing as Reference
        from docxtpl_native import Listing
        class CustomText(str):
            def replace(self, *args):
                return super().replace(*args)
        class Stringable:
            def __str__(self):
                return "<&'\">"
        for value in ("\ud800", "", "Café <&> ' \"\n\a", 0, None, True, Stringable(), CustomText("<&>")):
            with self.subTest(value=value):
                reference, candidate = Reference(value), Listing(value)
                self.assertEqual(candidate.xml, reference.xml)
                self.assertEqual(str(candidate), str(reference))
                self.assertEqual(candidate.__html__(), reference.__html__())
                self.assertEqual(candidate.__unicode__(), reference.__unicode__())
                candidate.xml = reference.xml = "changed<&"
                self.assertEqual(str(candidate), str(reference))
        for cls in (Reference, Listing):
            with self.assertRaises(TypeError):
                cls(b"bytes")



    def test_richtext_native_subset_matches_reference(self):
        from docxtpl import RichText as Reference
        from docxtpl_native import RichText, R
        self.assertIs(R, RichText)
        texts = ("", "Café <&> '\"", "\ud800", b"bytes", 123)
        properties = (
            {}, {"style": "Strong"}, {"color": "#aabbcc", "highlight": "#112233"},
            {"size": 18, "subscript": True, "superscript": True},
            {"bold": True, "italic": True, "rtl": True, "strike": True},
            {"underline": True}, {"underline": "wave"}, {"underline": "bad"},
            {"style": "X", "color": "#0", "bold": True, "lang": "pt-BR",
             "url_id": "rId3", "underline": "double", "size": 23},
            {"font": "eastAsia:Arial", "bold": True},
            {"style": "Strong", "font": "eastAsia:Arial", "color": "#AABBCC", "bold": True},
            {"size": -1},
        )
        for text in texts:
            for kwargs in properties:
                with self.subTest(text=text, kwargs=kwargs):
                    expected, actual = Reference(), RichText()
                    expected.add(text, **kwargs)
                    actual.add(text, **kwargs)
                    self.assertEqual(actual.xml, expected.xml)
                    self.assertEqual(str(actual), str(expected))
                    actual.add(RichText("reference"), bold=True)
                    expected.add(Reference("reference"), bold=True)
                    self.assertEqual(actual.xml, expected.xml)



    def test_richtext_uses_native_builder_for_supported_values(self):
        from unittest.mock import patch
        from docxtpl_native import RichText, _extension
        module = _extension()
        with patch.object(module, "add_richtext", wraps=module.add_richtext) as native:
            RichText("native", bold=True, color="123456")
            self.assertEqual(native.call_count, 1)
            RichText("native font", font="Arial")
            self.assertEqual(native.call_count, 2)



    def test_listing_preserves_replace_calls_and_exceptions(self):
        from docxtpl import Listing as Reference
        from docxtpl_native import Listing
        traces = []
        class CustomText(str):
            def replace(self, old, new):
                traces[-1].append((old, new))
                return CustomText(super().replace(old, new))
        reference_trace = []
        traces.append(reference_trace)
        reference = Reference(CustomText("<&'\""))
        candidate_trace = []
        traces.append(candidate_trace)
        candidate = Listing(CustomText("<&'\""))
        self.assertEqual(candidate.xml, reference.xml)
        self.assertEqual(candidate_trace, reference_trace)
        error = RuntimeError("custom replacement")
        class BrokenText(str):
            def replace(self, old, new):
                raise error
        for cls in (Reference, Listing):
            with self.assertRaises(RuntimeError) as captured:
                cls(BrokenText("x"))
            self.assertIs(captured.exception, error)

    def test_listing_uses_native_adapter_for_python_values(self):
        from unittest.mock import patch
        from docxtpl_native import Listing, _extension
        module = _extension()
        with patch.object(module, "escape_listing", wraps=module.escape_listing) as native:
            for value in ("<&", 5, "\ud800"):
                Listing(value)
            self.assertEqual(native.call_count, 3)

    def test_listing_unicode_and_markup_partitions(self):
        import random
        from docxtpl import Listing as Reference
        from docxtpl_native import Listing
        rng = random.Random(9481)
        alphabet = ["a", "é", "中", "&", "<", ">", "'", '"', "\n",
                    "\t", chr(0), chr(0xD800), chr(0x1F600)]
        for index in range(256):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(40)))
            with self.subTest(case=index):
                self.assertEqual(Listing(text).xml, Reference(text).xml)

    def test_listing_reuses_unescaped_exact_string(self):
        from docxtpl import Listing as Reference
        from docxtpl_native import Listing
        for text in ("".join(["unescaped", " text"]), "conteúdo sem tags"):
            with self.subTest(text=text):
                self.assertIs(Reference(text).xml, text)
                self.assertIs(Listing(text).xml, text)


    def test_richtext_unicode_and_property_combinations(self):
        import random
        from docxtpl import RichText as Reference
        from docxtpl_native import RichText
        rng = random.Random(84017)
        alphabet = ["a", "é", "中", "🐍", "&", "<", ">", "'", '"', "\n", "\t", chr(0)]
        for index in range(256):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(60)))
            props = {
                "style": rng.choice([None, "", "Strong"]),
                "font": rng.choice([None, "", "Arial", "eastAsia:SimSun"]),
                "color": rng.choice([None, "", "#aabbcc"]),
                "highlight": rng.choice([None, "", "#123456"]),
                "size": rng.choice([None, 0, 12, 24]),
                "bold": rng.choice([True, False]),
                "italic": rng.choice([True, False]),
                "rtl": rng.choice([True, False]),
                "underline": rng.choice([None, False, True, "wave", "invalid"]),
                "url_id": rng.choice([None, "", "rId7"]),
            }
            with self.subTest(case=index):
                expected, actual = Reference(), RichText()
                expected.add(text, **props)
                actual.add(text, **props)
                self.assertEqual(actual.xml, expected.xml)


if __name__=='__main__':unittest.main()

