"""Callback exception handlers retain Python's live globals and error state."""
import sys
import unittest
from contextlib import nullcontext
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
import jinja2.runtime as runtime
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate
from test_native_call_algorithms import native_dispatch_only, render


class NativeCallExceptionTests(unittest.TestCase):
    def test_undefined_observes_original_and_restores_outer_exception(self):
        def run(native):
            original = RuntimeError('outer')
            stopped = StopIteration('callback')
            trace = []
            class Undefined(jinja2.Undefined):
                def __init__(self, *args, **kwargs):
                    trace.append((sys.exception() is stopped, stopped.__context__ is original))
                    super().__init__(*args, **kwargs)
            def callback():
                raise stopped
            try:
                raise original
            except RuntimeError:
                output = render(native, jinja2.Environment(undefined=Undefined),
                                '{{ callback() }}', {'callback': callback})
                trace.append(sys.exception() is original)
            return output, trace
        expected = run(False)
        self.assertEqual(expected, ('', [(True, True), True]))
        self.assertEqual(run(True), expected)

    def test_undefined_failure_keeps_original_exception_context(self):
        def run(native):
            stopped = StopIteration('callback')
            failure = ValueError('undefined failed')
            class Undefined(jinja2.Undefined):
                def __init__(self, *args, **kwargs):
                    raise failure
            def callback():
                raise stopped
            try:
                render(native, jinja2.Environment(undefined=Undefined),
                       '{{ callback() }}', {'callback': callback})
            except ValueError as error:
                return error is failure, error.__context__ is stopped
            self.fail('undefined failure was ignored')
        self.assertEqual(run(False), (True, True))
        self.assertEqual(run(True), (True, True))

    def test_except_target_uses_globals_changed_by_callback(self):
        for target in (ValueError, (KeyError, ValueError), KeyError, object(), ((ValueError,),)):
            def run(native):
                failure = ValueError('callback')
                trace = []
                class Undefined(jinja2.Undefined):
                    def __init__(self, *args, **kwargs):
                        trace.append(sys.exception() is failure)
                        super().__init__(*args, **kwargs)
                def callback():
                    runtime.StopIteration = target
                    raise failure
                with patch.object(runtime, 'StopIteration', StopIteration, create=True):
                    try:
                        result = render(native, jinja2.Environment(undefined=Undefined),
                                        '{{ callback() }}', {'callback': callback})
                        return 'ok', result, trace
                    except Exception as error:
                        return type(error).__name__, str(error), error is failure, error.__context__ is failure, trace
            with self.subTest(target=target):
                self.assertEqual(run(True), run(False))

    def test_complete_document_sync_and_async_keeps_handled_exception(self):
        for asynchronous in (False, True):
            def run(native):
                failure = StopIteration('callback')
                trace = []
                class Undefined(jinja2.Undefined):
                    def __init__(self, *args, **kwargs):
                        trace.append(sys.exception() is failure)
                        super().__init__(*args, **kwargs)
                def callback():
                    raise failure
                environment = jinja2.Environment(undefined=Undefined, enable_async=asynchronous)
                with native_dispatch_only() if native else nullcontext():
                    blob = generate(docxtpl_native if native else docxtpl,
                                    'before {{ callback() }} after', {'callback': callback}, environment)
                return blob, trace
            with self.subTest(asynchronous=asynchronous):
                reference, expected = run(False)
                candidate, actual = run(True)
                self.assertTrue(expected and all(expected))
                self.assertEqual(actual, expected)
                difference = compare_docx(reference, candidate)
                self.assertTrue(difference['equivalent'], difference)


if __name__ == '__main__':
    unittest.main()
