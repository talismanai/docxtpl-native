"""Internal special-value collection and public instance protocols."""

import io
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import docxtpl_native
from docxtpl_native.bridge import collect_specials


class SpecialProbeTests(unittest.TestCase):
    def test_internal_collection_recognizes_real_image_subclasses(self) -> None:
        class Image(docxtpl_native.InlineImage):
            pass

        values = [Image(None, io.BytesIO(b'image')), docxtpl_native.InlineImage(None, io.BytesIO(b'image'))]
        result = docxtpl_native._extension().collect_specials(
            values, {}, (docxtpl_native.InlineImage, docxtpl_native.Subdoc),
            lambda _: 'converted', docxtpl_native._OriginalSubdoc,
            docxtpl_native._OriginalInlineImage, True,
        )
        self.assertEqual([item['identity'] for item in result], [id(values[1]), id(values[0])])

    def test_public_collection_keeps_reported_class_protocol(self) -> None:
        class Proxy:
            @property
            def __class__(self) -> type[Any]:
                return docxtpl_native.InlineImage

        proxy = Proxy()
        marker = object()
        with patch.object(docxtpl_native, '_value', lambda _: marker):
            result = collect_specials([proxy], SimpleNamespace(globals={}))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['identity'], id(proxy))
        self.assertIs(result[0]['value'], marker)

    def test_internal_collection_respects_a_replaced_image_class(self) -> None:
        # The concrete-type shortcut applies only to the package's own classes;
        # a replacement keeps its instance-check protocol.
        class Proxy:
            selected = True

        class Checker(type):
            def __instancecheck__(cls, instance: object) -> bool:
                return bool(getattr(instance, 'selected', False))

        class Replacement(metaclass=Checker):
            pass

        proxy = Proxy()
        result = docxtpl_native._extension().collect_specials(
            [proxy], {}, (Replacement, docxtpl_native.Subdoc),
            lambda _: 'converted', docxtpl_native._OriginalSubdoc,
            docxtpl_native._OriginalInlineImage, True,
        )
        self.assertEqual(result, [{'identity': id(proxy), 'value': 'converted'}])

if __name__ == '__main__':
    unittest.main()
