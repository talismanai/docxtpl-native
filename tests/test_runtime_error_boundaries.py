"""Iteration exceptions must not be mistaken for successful render completion."""
import unittest
import docxtpl_native
import jinja2
from jinja2.ext import Extension
from docxtpl_native.bridge import BridgeConfig
from native_audit import forbid
from paritylab.runtime_error_probes import make_case, exception_observation, PROBES


def block_executors():
    return forbid('jinja2.Template.render', 'jinja2.Template.render_async', 'jinja2.Template.generate_async',
                  message='Python executor called')


class RuntimeErrorBoundaryTests(unittest.TestCase):
    def native(self, environment, source, values):
        with block_executors():
            return docxtpl_native._extension().PythonRenderSession(
                BridgeConfig(environment, False), values).render('case', source)

    def test_runtime_exception_chains(self):
        for asynchronous in (False, True):
            for origin in ('filter', 'callback', 'str'):
                for exception_type in (StopIteration, StopAsyncIteration):
                    def run(native):
                        environment, source, values, failure = make_case(asynchronous, origin, exception_type)
                        try:
                            output = (self.native(environment, source, values) if native
                                      else environment.from_string(source).render(values))
                            return {'status': 'ok', 'text': output}
                        except BaseException as error:
                            return exception_observation(error, failure)
                    with self.subTest(asynchronous=asynchronous, origin=origin, exception=exception_type):
                        expected = PROBES['async_runtime_iteration_errors' if asynchronous else 'runtime_iteration_errors'].expected
                        self.assertEqual(run(False), expected[origin + ':' + exception_type.__name__])
                        self.assertEqual(run(True), run(False))

    def test_compilation_errors_keep_original_identity(self):
        for asynchronous in (False, True):
            for exception_type in (StopIteration, StopAsyncIteration):
                def run(native):
                    failure = exception_type('compile marker')
                    class Failing(Extension):
                        def preprocess(self, source, name, filename=None):
                            raise failure
                    environment = jinja2.Environment(extensions=[Failing], enable_async=asynchronous)
                    try:
                        if native:
                            self.native(environment, '{{ value }}', {'value': 42})
                        else:
                            environment.from_string('{{ value }}').render({'value': 42})
                    except BaseException as error:
                        return exception_observation(error, failure)
                    raise AssertionError('compilation did not fail')
                with self.subTest(asynchronous=asynchronous, exception=exception_type):
                    reference = run(False)
                    self.assertTrue(reference['same_instance'])
                    self.assertEqual(run(True), reference)

    def test_context_snapshot_has_its_own_coroutine_boundary(self):
        for asynchronous in (False, True):
            for exception_type in (StopIteration, StopAsyncIteration):
                def run(native):
                    failure = exception_type('snapshot marker')
                    class Mapping:
                        def keys(self):
                            raise failure
                        def __getitem__(self, key):
                            raise AssertionError('values must not be read after keys failed')
                    environment = jinja2.Environment(enable_async=asynchronous)
                    try:
                        if native:
                            self.native(environment, '{{ value }}', Mapping())
                        else:
                            environment.from_string('{{ value }}').render(Mapping())
                    except BaseException as error:
                        return exception_observation(error, failure)
                    raise AssertionError('snapshot error became success')
                with self.subTest(asynchronous=asynchronous, exception=exception_type):
                    reference = run(False)
                    if asynchronous and exception_type is StopIteration:
                        self.assertEqual(reference['message'], 'coroutine raised StopIteration')
                        self.assertTrue(reference['cause_same'])
                    else:
                        self.assertTrue(reference['same_instance'])
                    self.assertEqual(run(True), reference)

    def test_complete_document_probes(self):
        # Document equivalence with docxtpl: test_audited_documents_reference.
        for name, probe in PROBES.items():
            with block_executors():
                native = probe.execute(docxtpl_native)
            with self.subTest(name=name):
                self.assertEqual(native.observation, probe.expected)
                self.assertEqual(len(native.outputs), 1)

    def test_bridge_does_not_evaluate_exception_string(self):
        for asynchronous in (False, True):
            for phase in ('compile', 'runtime'):
                def run(native):
                    trace = []
                    class Failure(RuntimeError):
                        def __str__(self):
                            trace.append('str')
                            return 'failure marker'
                    failure = Failure()
                    class Failing(Extension):
                        def preprocess(self, source, name, filename=None):
                            raise failure
                    def callback():
                        raise failure
                    environment = jinja2.Environment(enable_async=asynchronous,
                        extensions=[Failing] if phase == 'compile' else [])
                    try:
                        if native:
                            self.native(environment, '{{ callback() }}', {'callback': callback})
                        else:
                            environment.from_string('{{ callback() }}').render({'callback': callback})
                    except BaseException as error:
                        return error is failure, trace
                    raise AssertionError('exception was lost')
                with self.subTest(asynchronous=asynchronous, phase=phase):
                    self.assertEqual(run(False), (True, []))
                    self.assertEqual(run(True), (True, []))
