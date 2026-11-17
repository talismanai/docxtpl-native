"""Save the exposed graph once, with observable hooks and destination protocols."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from docx import Document
import docxtpl
import docxtpl_native
import jinja2
from PIL import Image
from paritylab.cases import fixture, inputs
from paritylab.compare import compare_docx


def template_source():
    document = Document()
    document.add_paragraph('Hello {{ name }}')
    source = io.BytesIO()
    document.save(source)
    return source.getvalue()


class LiveSavePipelineTests(unittest.TestCase):
    def test_model_save_replaced_before_the_first_save_is_called(self):
        # In a fresh process the first save happens inside the replacement:
        # the save chain must have been recorded when the package loaded.
        import subprocess
        import sys
        program = (
            'from paritylab.live_save_probes import live_save_hooks\n'
            'import docxtpl_native\n'
            'print(live_save_hooks(docxtpl_native).observation["trace"])\n'
        )
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True, check=True,
                                env={'PYTHONPATH': str(root / 'python'), 'PATH': ''}, cwd=root)
        self.assertEqual(result.stdout.strip().splitlines()[-1],
                         "[['before', True, False], ['model', True], ['after', True, False]]")

    def test_picture_replacements_update_retained_parts_across_saves(self):
        def png(color):
            output = io.BytesIO()
            Image.new('RGB', (2, 2), color).save(output, format='PNG')
            return output.getvalue()
        original = Document()
        original.add_paragraph().add_run().add_picture(io.BytesIO(png('red')))
        source = io.BytesIO()
        original.save(source)
        for key, allow_missing in (('image.png', False), ('missing', False), ('missing', True)):
            def run(engine):
                template = engine.DocxTemplate(io.BytesIO(source.getvalue()))
                template.render({}, jinja_env=jinja2.Environment())
                image = next(rel.target_part for rel in template.docx.part.rels.values()
                             if rel.reltype.endswith('/image'))
                results = []
                for color in ('blue', 'green'):
                    template.pics_to_replace = {key: png(color)}
                    template.allow_missing_pics = allow_missing
                    output = io.BytesIO()
                    try:
                        with patch('zipfile.time.time', return_value=1700000000):
                            template.save(output)
                        error = None
                    except ValueError as failure:
                        error = (type(failure).__name__, str(failure))
                    results.append((image.blob == png(color), sorted(template.pic_map), error,
                                    template.is_saved, output.getvalue()))
                return results
            with self.subTest(key=key, allow_missing=allow_missing):
                actual, expected = run(docxtpl_native), run(docxtpl)
                for candidate, reference in zip(actual, expected):
                    self.assertEqual(candidate[:-1], reference[:-1])
                    self.assertTrue(candidate[-1] == reference[-1], 'archive bytes differ')

    def test_file_save_and_zip_replacements_preserve_phase_order(self):
        source = template_source()
        with tempfile.TemporaryDirectory() as root:
            for stream in (False, True):
                def run(engine):
                    template = engine.DocxTemplate(io.BytesIO(source))
                    template.render({'name': 'Ada'}, jinja_env=jinja2.Environment())
                    # Substitution occurs after the exposed model is serialized.
                    template.zipname_to_replace['word/document.xml'] = b'<replacement/>'
                    output = io.BytesIO() if stream else Path(root) / (engine.__name__ + '.docx')
                    with patch('zipfile.time.time', return_value=1700000000) as clock:
                        template.save(output)
                    return (output.getvalue() if stream else output.read_bytes(), clock.call_count,
                            output.tell() if stream else None, template.is_saved,
                            template.docx.paragraphs[0].text)
                with self.subTest(stream=stream):
                    actual, expected = run(docxtpl_native), run(docxtpl)
                    self.assertEqual(actual[1:], expected[1:])
                    self.assertTrue(actual[0] == expected[0], 'archive bytes differ')

    def test_postprocessing_error_keeps_written_output_and_unsaved_state(self):
        source = template_source()
        def run(engine):
            template = engine.DocxTemplate(io.BytesIO(source))
            template.render({'name': 'Ada'}, jinja_env=jinja2.Environment())
            failure = ValueError('postprocessing failed')
            target = io.BytesIO()
            trace = []
            def after(output):
                trace.append((output is target, template.is_saved, bool(target.getvalue())))
                raise failure
            template.post_processing = after
            with patch('zipfile.time.time', return_value=1700000000):
                try:
                    template.save(target)
                except ValueError as error:
                    same_error = error is failure
                else:
                    same_error = False
            return target.getvalue(), trace, same_error, template.is_saved
        actual, expected = run(docxtpl_native), run(docxtpl)
        self.assertEqual(actual[1:], expected[1:])
        self.assertTrue(actual[0] == expected[0], 'archive bytes differ')

    def test_hooks_mutate_the_retained_graph_before_serialization(self):
        source = template_source()
        def run(engine):
            template = engine.DocxTemplate(io.BytesIO(source))
            template.render({'name': 'Ada'}, jinja_env=jinja2.Environment())
            document = template.docx
            trace = []
            target = io.BytesIO()
            def before():
                trace.append(('before', template.docx is document, template.is_saved))
                document.paragraphs[0].text = 'Changed before save'
            def after(output):
                trace.append(('after', output is target, template.is_saved))
            template.pre_processing = before
            template.post_processing = after
            with patch('zipfile.time.time', return_value=1700000000):
                template.save(target)
            return target.getvalue(), trace, template.is_saved, document.paragraphs[0].text
        expected = run(docxtpl)
        self.assertEqual(expected[1], [('before', True, False), ('after', True, False)])
        actual = run(docxtpl_native)
        self.assertEqual(actual[1:], expected[1:])
        self.assertTrue(actual[0] == expected[0], 'archive bytes differ')

    def test_model_save_receives_the_original_destination(self):
        source = template_source()
        def run(engine):
            template = engine.DocxTemplate(io.BytesIO(source))
            template.render({'name': 'Ada'}, jinja_env=jinja2.Environment())
            target = io.BytesIO()
            trace = []
            document_type = type(template.docx)
            original = document_type.save
            def save(document, destination):
                trace.append((document is template.docx, destination is target))
                return original(document, destination)
            with patch.object(document_type, 'save', save), patch('zipfile.time.time', return_value=1700000000):
                template.save(target)
            return target.getvalue(), trace, target.tell()
        expected = run(docxtpl)
        self.assertEqual(expected[1], [(True, True)])
        actual = run(docxtpl_native)
        self.assertEqual(actual[1:], expected[1:])
        self.assertTrue(actual[0] == expected[0], 'archive bytes differ')

    def test_stream_operations_and_partial_write_errors(self):
        source = template_source()
        for fail_at in (None, 4):
            def run(engine):
                template = engine.DocxTemplate(io.BytesIO(source))
                template.render({'name': 'Ada'}, jinja_env=jinja2.Environment())
                trace = []
                failure = ValueError('write failed')
                writes = 0
                class Stream(io.BytesIO):
                    def write(self, value):
                        nonlocal writes
                        writes += 1
                        trace.append(('write', len(value)))
                        if writes == fail_at:
                            raise failure
                        return super().write(value)
                    def seek(self, *args):
                        trace.append(('seek', args))
                        return super().seek(*args)
                    def flush(self):
                        trace.append(('flush',))
                        return super().flush()
                target = Stream()
                same_error = None
                with patch('zipfile.time.time', return_value=1700000000):
                    try:
                        template.save(target)
                    except ValueError as error:
                        same_error = error is failure
                observed = target.getvalue(), trace[:], target.tell(), same_error, template.is_saved
                # Observe failure before releasing reference ZipFile tracebacks.
                failure.__traceback__ = None
                return observed
            with self.subTest(fail_at=fail_at):
                actual, expected = run(docxtpl_native), run(docxtpl)
                self.assertEqual(actual[1:], expected[1:])
                self.assertTrue(actual[0] == expected[0], 'partial archive bytes differ')

    def test_preprocessing_error_prevents_save_and_postprocessing(self):
        source = template_source()
        def run(engine):
            template = engine.DocxTemplate(io.BytesIO(source))
            template.render({'name': 'Ada'}, jinja_env=jinja2.Environment())
            trace = []
            failure = ValueError('preprocessing failed')
            def before():
                trace.append('before')
                raise failure
            template.pre_processing = before
            template.post_processing = lambda output: trace.append('after')
            target = io.BytesIO()
            try:
                template.save(target)
            except ValueError as error:
                same_error = error is failure
            else:
                same_error = False
            return trace, same_error, target.getvalue(), template.is_saved
        self.assertEqual(run(docxtpl), (['before'], True, b'', False))
        actual, expected = run(docxtpl_native), run(docxtpl)
        self.assertEqual(actual[:2], expected[:2])
        self.assertEqual(actual[3], expected[3])
        self.assertTrue(actual[2] == expected[2], 'archive bytes differ')

    def test_live_documents_do_not_reparse_or_recompress_after_model_save(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ('pass_context', 'loader', 'image', 'subdocument'):
                directory = Path(root) / name
                source = fixture(name, directory)
                outputs = []
                for engine in (docxtpl, docxtpl_native):
                    document = engine.DocxTemplate(str(source))
                    context, environment, autoescape = inputs(name, engine, document, directory)
                    document.render(context, jinja_env=environment, autoescape=autoescape)
                    output = io.BytesIO()
                    with patch.object(docxtpl_native._extension(), 'process_docx', side_effect=AssertionError('redundant package serialization')):
                        with patch('zipfile.time.time', return_value=1700000000):
                            document.save(output)
                    outputs.append(output.getvalue())
                with self.subTest(case=name):
                    comparison = compare_docx(*outputs)
                    self.assertTrue(comparison['equivalent'], comparison)
                    self.assertTrue(outputs[0] == outputs[1], 'archive bytes differ')
