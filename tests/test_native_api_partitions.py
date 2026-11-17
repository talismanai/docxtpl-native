"""Public API behaviours found by the API partition probes."""
import io
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

import docxtpl_native
from docx import Document
from docxtpl_native import _read


def source() -> bytes:
    document = Document()
    document.add_paragraph('Hi {{ name }}')
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


class NativeApiPartitionTests(unittest.TestCase):
    def test_unreadable_source_raises_the_archives_missing_method(self) -> None:
        with self.assertRaisesRegex(AttributeError, "'object' object has no attribute 'seek'"):
            _read(object())

        class SeekOnly:
            def seek(self, *args: object) -> None:
                pass
        with self.assertRaisesRegex(AttributeError, "'SeekOnly' object has no attribute 'tell'"):
            _read(SeekOnly())
        with self.assertRaisesRegex(AttributeError, "has no attribute 'seek'"):
            docxtpl_native.DocxTemplate(object()).render({})

    def test_save_targets_follow_zipfile(self) -> None:
        document = docxtpl_native.DocxTemplate(io.BytesIO(source()))
        document.render({'name': 'Ada'})
        with self.assertRaisesRegex(AttributeError, "'object' object has no attribute 'write'"):
            document.save(object())
        self.assertFalse(document.is_saved)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'out.docx'

            class Target(os.PathLike):
                def __fspath__(self) -> str:
                    return str(path)

                def write(self, data: bytes) -> None:
                    raise AssertionError('a path-like target is a path')
            document.save(Target())
            self.assertEqual(Document(str(path)).paragraphs[0].text, 'Hi Ada')

    def test_non_string_hyperlink_target_is_kept_as_python_docx_keeps_it(self) -> None:
        document = docxtpl_native.DocxTemplate(io.BytesIO(source()))
        first = document.build_url_id(1)
        self.assertEqual(document.build_url_id(1), first)
        self.assertNotEqual(document.build_url_id('https://example.com'), first)
        self.assertEqual(document.docx.part.rels[first].target_ref, 1)

    def test_failed_render_keeps_the_parsed_body_while_its_traceback_lives(self) -> None:
        import gc
        document = Document()
        document.add_paragraph('{{ value }}')
        document.sections[0].footer.paragraphs[0].text = '{{ fail() }}'
        buffer = io.BytesIO()
        document.save(buffer)
        template = docxtpl_native.DocxTemplate(io.BytesIO(buffer.getvalue()))
        model = template.get_docx()

        def fail() -> str:
            raise ValueError('footer fails')
        kept: list[BaseException] = []
        try:
            template.render({'value': 'V', 'fail': fail})
        except ValueError as error:
            kept.append(error)
        # as docxtpl's render frame holds the body it parsed
        self.assertEqual(type(model.element.body).__name__, '_Element')
        kept.clear()
        gc.collect()
        self.assertEqual(model.paragraphs[0].text, 'V')

    def test_failed_render_releases_its_exception_and_template(self) -> None:
        import gc
        import weakref
        document = Document()
        document.add_paragraph('{{ value }}')
        document.sections[0].footer.paragraphs[0].text = '{{ fail() }}'
        buffer = io.BytesIO()
        document.save(buffer)

        class Marker(ValueError):
            pass

        def fail() -> str:
            raise Marker('footer fails')
        def run(expose: bool) -> weakref.ref[Any]:
            template = docxtpl_native.DocxTemplate(io.BytesIO(buffer.getvalue()))
            if expose:
                template.get_docx()
            with self.assertRaises(Marker):
                template.render({'value': 'V', 'fail': fail})
            return weakref.ref(template)
        for expose in (False, True):
            reference = run(expose)
            gc.collect()
            self.assertIsNone(reference(), expose)
            self.assertFalse([item for item in gc.get_objects() if isinstance(item, Marker)], expose)

    def test_rich_values_own_their_representation(self) -> None:
        for cls in (docxtpl_native.RichText, docxtpl_native.RichTextParagraph, docxtpl_native.Listing):
            for name in ('__str__', '__html__'):
                self.assertEqual(getattr(cls, name).__qualname__, f'{cls.__name__}.{name}')
        representation: Any = docxtpl_native.RichText('x').__str__
        with self.assertRaisesRegex(TypeError, r'RichText\.__str__\(\) takes 1 positional argument'):
            representation(1)


if __name__ == '__main__':
    unittest.main()
