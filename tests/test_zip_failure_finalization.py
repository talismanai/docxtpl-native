"""ZIP finalization follows the lifetime of the failed save's traceback."""
import gc
import io
import unittest
from unittest.mock import patch
import zipfile
from docx import Document
import docxtpl
import docxtpl_native


class ZipFailureFinalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        document = Document()
        document.add_paragraph('Hello {{ name }}')
        source = io.BytesIO()
        document.save(source)
        cls.source = source.getvalue()
        with zipfile.ZipFile(source) as archive:
            cls.entries = len(archive.infolist())

    def failed_save(self, engine, fail_at):
        template = engine.DocxTemplate(io.BytesIO(self.source))
        template.render({'name': 'Ada'})
        target = io.BytesIO()
        failure = ValueError('clock failed')
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls == fail_at:
                raise failure
            return 1700000000
        with patch('zipfile.time.time', side_effect=clock):
            try:
                template.save(target)
            except ValueError as error:
                self.assertIs(error, failure)
            else:
                self.fail('expected clock failure')
        self.assertEqual(calls, fail_at)
        self.assertFalse(template.is_saved)
        return target, failure

    def test_completed_prefix_is_finalized_when_traceback_is_released(self):
        for fail_at in (1, 2, self.entries):
            def run(engine):
                target, failure = self.failed_save(engine, fail_at)
                before = target.getvalue(), target.tell()
                failure.__traceback__ = None
                gc.collect()
                return before, (target.getvalue(), target.tell())
            with self.subTest(fail_at=fail_at):
                expected, actual = run(docxtpl), run(docxtpl_native)
                for reference, candidate in zip(expected, actual):
                    self.assertEqual(candidate[1], reference[1])
                    self.assertTrue(candidate[0] == reference[0], 'partial ZIP bytes differ')
                with zipfile.ZipFile(io.BytesIO(actual[1][0])) as archive:
                    self.assertEqual(len(archive.infolist()), fail_at - 1)
                    self.assertIsNone(archive.testzip())

    def test_retained_traceback_delays_finalization(self):
        def run(engine):
            target, failure = self.failed_save(engine, 2)
            traceback = failure.__traceback__
            before = target.getvalue()
            failure.__traceback__ = None
            gc.collect()
            unchanged = target.getvalue() == before
            del traceback
            gc.collect()
            return unchanged, target.getvalue(), target.tell()
        expected, actual = run(docxtpl), run(docxtpl_native)
        self.assertTrue(expected[0])
        self.assertEqual(actual[0], expected[0])
        self.assertEqual(actual[2], expected[2])
        self.assertTrue(actual[1] == expected[1], 'final archive bytes differ')

    def test_finalizer_uses_current_stream_protocols(self):
        def run(engine):
            target, failure = self.failed_save(engine, 2)
            trace = []
            def write(data):
                trace.append(('write', len(data)))
                return io.BytesIO.write(target, data)
            def seek(*args):
                trace.append(('seek', args))
                return io.BytesIO.seek(target, *args)
            def flush():
                trace.append(('flush',))
                return io.BytesIO.flush(target)
            target.write, target.seek, target.flush = write, seek, flush
            failure.__traceback__ = None
            gc.collect()
            return trace, target.getvalue(), target.tell()
        expected, actual = run(docxtpl), run(docxtpl_native)
        self.assertEqual(actual[0], expected[0])
        self.assertEqual(actual[2], expected[2])
        self.assertTrue(actual[1] == expected[1], 'final archive bytes differ')

    def test_successful_save_does_not_finalize_again(self):
        template = docxtpl_native.DocxTemplate(io.BytesIO(self.source))
        template.render({'name': 'Ada'})
        target = io.BytesIO()
        with patch('zipfile.time.time', return_value=1700000000):
            template.save(target)
        before = target.getvalue(), target.tell()
        gc.collect()
        self.assertEqual(target.tell(), before[1])
        self.assertTrue(target.getvalue() == before[0])

    def test_unreachable_exception_cycle_releases_the_writer(self):
        def run(engine):
            target, failure = self.failed_save(engine, 2)
            before = target.getvalue()
            failure.cycle = failure
            del failure
            gc.collect()
            return target.getvalue() != before, target.getvalue(), target.tell()
        expected, actual = run(docxtpl), run(docxtpl_native)
        self.assertTrue(expected[0])
        self.assertEqual(actual[0], expected[0])
        self.assertEqual(actual[2], expected[2])
        self.assertTrue(actual[1] == expected[1], 'final archive bytes differ')
