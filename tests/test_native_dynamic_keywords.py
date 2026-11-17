"""Unpacked keywords must not recurse into foreign string hashing in the VM."""
import json
import os
import subprocess
import sys
import unittest

import docxtpl
import docxtpl_native
import jinja2
from jinja2.sandbox import SandboxedEnvironment
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate
from paritylab.dynamic_keyword_probes import observe, VARIANTS, EXPECTED, PROBES
from test_native_call_algorithms import native_dispatch_only
from test_native_sandbox_call import native_sandbox_only


def check_family(asynchronous, sandboxed):
    for variant in VARIANTS:
        expected, reference = observe(docxtpl, asynchronous, sandboxed, variant)
        assert expected == EXPECTED[sandboxed][variant], (variant, expected)
        with native_dispatch_only(), native_sandbox_only():
            actual, candidate = observe(docxtpl_native, asynchronous, sandboxed, variant)
        assert actual == expected, (variant, expected, actual)
        assert (reference is None) == (candidate is None), variant
        if reference is not None:
            difference = compare_docx(reference, candidate)
            assert difference['equivalent'], (variant, difference)
    print(json.dumps({'cases': 10, 'passed': 10}))


class DynamicKeywordTests(unittest.TestCase):
    def test_keyword_errors_chain_and_restore_the_handled_outer_exception(self):
        cases = [
            ('{{ callback(**kwargs) }}', 42),
            ('{{ callback(label="first", **kwargs) }}', {'label': 'done'}),
            ('{{ callback(**kwargs) }}', {'_Context__self': 1}),
            ('{{ callback(**kwargs) }}', {42: 1}),
            ('{{ value is integer(**kwargs) }}', {'value': 1}),
            ('{{ value is integer(**kwargs) }}', {'unknown': 1}),
            ('{{ value is integer(**kwargs) }}', {42: 1}),
            ('{{ value is sameas }}', {}),
            ('{{ value is integer(value) }}', {}),
        ]
        for source, kwargs in cases:
            def execute(engine):
                outer = RuntimeError('outer')
                try: raise outer
                except RuntimeError:
                    try:
                        generate(engine, source, {'callback': lambda **kw: 'unexpected',
                                                 'value': 41, 'kwargs': kwargs}, jinja2.Environment())
                    except BaseException as error:
                        result = (type(error).__name__, str(error), error.__context__ is outer,
                                  sys.exception() is error)
                    else: self.fail('keyword binding error was suppressed')
                    return result + (sys.exception() is outer,)
            with self.subTest(source=source, kwargs=kwargs):
                expected = execute(docxtpl)
                # In 3.11 an already handled exception makes the duplicate
                # KeyError normalized before format_kwargs_error sees it.
                expected_type = ('KeyError' if sys.version_info < (3, 12)
                                 and source == '{{ callback(label="first", **kwargs) }}' else 'TypeError')
                self.assertEqual(expected[0], expected_type)
                self.assertEqual(expected[2:], (True, True, True))
                self.assertEqual(execute(docxtpl_native), expected)

    def test_mapping_key_error_translation_matches_interpreter_version(self):
        for sandboxed in (False, True):
            def run(engine):
                trace = []
                class Failure(KeyError):
                    def __getattribute__(self, name):
                        if name == 'args':
                            trace.append('args hook')
                            raise AssertionError('exception attribute hook executed')
                        return super().__getattribute__(name)
                class Mapping:
                    def keys(self): return ['label']
                    def __getitem__(self, key): raise Failure(key)
                environment = (SandboxedEnvironment if sandboxed else jinja2.Environment)()
                try:
                    generate(engine, '{{ callback(**kwargs) }}',
                             {'callback': lambda **kwargs: 'unexpected', 'kwargs': Mapping()}, environment)
                except BaseException as error:
                    return type(error).__name__, str(error), trace
                self.fail('mapping error was suppressed')
            with self.subTest(sandboxed=sandboxed):
                expected = run(docxtpl)
                self.assertEqual(expected[0], 'TypeError' if sys.version_info >= (3, 12) else 'Failure')
                self.assertEqual(expected[2], [])
                self.assertEqual(run(docxtpl_native), expected)
        probe = PROBES['dynamic_keywords_key_error']
        reference = probe.execute(docxtpl)
        self.assertEqual(reference.observation, probe.expected)
        with native_dispatch_only(): native = probe.execute(docxtpl_native)
        self.assertEqual(native.observation, probe.expected)
        self.assertEqual(native.outputs.keys(), reference.outputs.keys())
        for filename, blob in native.outputs.items():
            self.assertTrue(compare_docx(reference.outputs[filename], blob)['equivalent'])

    def test_filter_and_test_callbacks_keep_keyword_protocols(self):
        for kind in ('filter', 'test'):
            probe = PROBES['dynamic_keywords_' + kind]
            with self.subTest(kind=kind):
                reference = probe.execute(docxtpl)
                self.assertEqual(reference.observation, probe.expected)
                with native_dispatch_only():
                    native = probe.execute(docxtpl_native)
                self.assertEqual(native.observation, reference.observation)
                self.assertEqual(native.outputs.keys(), reference.outputs.keys())
                for filename, blob in native.outputs.items():
                    self.assertTrue(compare_docx(reference.outputs[filename], blob)['equivalent'])

    def test_complete_documents_and_errors_in_isolated_processes(self):
        # A recursion/crash must fail a regression without killing the suite.
        for asynchronous in (False, True):
            for sandboxed in (False, True):
                with self.subTest(asynchronous=asynchronous, sandboxed=sandboxed):
                    code = ('from test_native_dynamic_keywords import check_family; '
                            f'check_family({asynchronous!r}, {sandboxed!r})')
                    result = subprocess.run([sys.executable, '-X', 'faulthandler', '-c', code],
                                            capture_output=True, text=True, timeout=60,
                                            env={**os.environ, 'PYTHONPATH': os.pathsep.join(sys.path)})
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(json.loads(result.stdout), {'cases': 10, 'passed': 10})


if __name__ == '__main__':
    unittest.main()
