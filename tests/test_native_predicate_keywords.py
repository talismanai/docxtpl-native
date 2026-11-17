"""Fixed-signature native predicates retain Python keyword binding protocols."""
from contextlib import contextmanager, nullcontext
import sys
import unittest

import docxtpl
import docxtpl_native
import jinja2
from jinja2.defaults import DEFAULT_TESTS
from paritylab.compare import compare_docx
from paritylab.dynamic_keyword_probes import PROBES, protocol_key
from paritylab.protocol_base import generate
from test_native_call_algorithms import native_dispatch_only

STANDARD_CODES = {function.__code__ for function in DEFAULT_TESTS.values()
                  if hasattr(function, '__code__')}


@contextmanager
def function_attributes(function, **updates):
    original = {name: getattr(function, name) for name in updates}
    try:
        for name, value in updates.items(): setattr(function, name, value)
        yield
    finally:
        # Function slots cannot be removed by mock.patch's inherited-attribute
        # cleanup; restore the original slot values even after an error.
        for name, value in original.items(): setattr(function, name, value)


@contextmanager
def native_predicates_only():
    with native_dispatch_only():
        previous = sys.getprofile()
        def profile(frame, event, argument):
            if event == 'call' and frame.f_code in STANDARD_CODES:
                raise AssertionError('standard Python predicate executed')
        sys.setprofile(profile)
        try: yield
        finally: sys.setprofile(previous)


def run(engine, source, values, trace, environment=None, failure=None):
    try:
        with native_predicates_only() if engine is docxtpl_native else nullcontext():
            blob = generate(engine, source, values, environment)
    except BaseException as error:
        return {'trace': trace[:], 'error': [type(error).__name__, str(error), error is failure]}, None
    return {'trace': trace[:], 'error': None}, blob


class PredicateKeywordTests(unittest.TestCase):
    def check(self, execute):
        expected, reference = execute(docxtpl)
        actual, native = execute(docxtpl_native)
        self.assertEqual(actual, expected)
        self.assertEqual(native is None, reference is None)
        if reference is not None:
            difference = compare_docx(reference, native)
            self.assertTrue(difference['equivalent'], difference)
        return expected

    def test_permanent_predicate_key_protocol_oracle(self):
        probe = PROBES['dynamic_keywords_predicate']
        reference = probe.execute(docxtpl)
        self.assertEqual(reference.observation, probe.expected)
        with native_predicates_only(): native = probe.execute(docxtpl_native)
        self.assertEqual(native.observation, probe.expected)
        self.assertTrue(compare_docx(reference.outputs['document.docx'], native.outputs['document.docx'])['equivalent'])

    def test_fixed_signatures_compare_keys_and_report_errors(self):
        for name in ('defined', 'undefined', 'none', 'boolean', 'false', 'true', 'integer', 'float', 'string', 'sameas'):
            for field in ('value', 'other', 'unknown', 42):
                def execute(engine):
                    trace = []
                    key = protocol_key(field, trace) if isinstance(field, str) else field
                    value = object()
                    keywords = {key: value}
                    trace.clear()
                    return run(engine, '{{ value is ' + name + '(**kwargs) }}',
                               {'value': value, 'kwargs': keywords}, trace)
                with self.subTest(name=name, field=field): self.check(execute)

    def test_keyword_matching_does_not_hash_and_preserves_comparison_errors(self):
        for fails in (False, True):
            def execute(engine):
                trace = []
                active = False
                failure = ValueError('comparison failed')
                class Key(str):
                    def __hash__(self):
                        if active: raise AssertionError('keyword was rehashed')
                        return str.__hash__(self)
                    def __eq__(self, other):
                        trace.append(other)
                        if fails: raise failure
                        return str.__eq__(self, other)
                value = object()
                kwargs = {Key('other'): value}
                active = True
                return run(engine, '{{ value is sameas(**kwargs) }}',
                           {'value': value, 'kwargs': kwargs}, trace, failure=failure)
            with self.subTest(fails=fails): self.check(execute)

    def test_keyword_values_remain_opaque_until_the_predicate_uses_them(self):
        def execute(engine):
            trace = []
            class Value:
                @property
                def __class__(self):
                    trace.append('class')
                    return object
            return run(engine, '{{ value is sameas(**kwargs) }}',
                       {'value': 41, 'kwargs': {'other': Value()}}, trace)
        self.assertEqual(self.check(execute), {'trace': [], 'error': None})

    def test_live_defaults_and_qualified_error_names(self):
        function = DEFAULT_TESTS['sameas']
        for defaults_count in (0, 1, 3):
            for extra in (False, True):
                def execute(engine):
                    value = object()
                    defaults = (value,) * defaults_count if defaults_count else None
                    with function_attributes(function, __defaults__=defaults, __qualname__='custom_\ud800'):
                        source = '{{ value is sameas(value, value) }}' if extra else '{{ value is sameas }}'
                        return run(engine, source, {'value': value}, [])
                with self.subTest(defaults=defaults_count, extra=extra): self.check(execute)

    def test_function_name_precedes_keyword_string_conversion(self):
        function = DEFAULT_TESTS['sameas']
        def execute(engine):
            trace = []
            class Key(str):
                __hash__ = str.__hash__
                def __str__(self):
                    trace.append('str')
                    function.__qualname__ = 'changed'
                    return 'rendered_\ud800'
            with function_attributes(function, __qualname__='before_\ud801'):
                return run(engine, '{{ value is sameas(**kwargs) }}',
                           {'value': 41, 'kwargs': {Key('unknown'): None}}, trace)
        self.check(execute)

    def test_code_mutation_before_and_during_parameter_matching(self):
        function = DEFAULT_TESTS['sameas']
        def replacement(value, other): return 'changed'
        for phase in ('mapping', 'comparison', 'before_render'):
            def execute(engine):
                trace = []
                value = object()
                class Key(str):
                    __hash__ = str.__hash__
                    def __eq__(self, other):
                        trace.append(other)
                        if phase == 'comparison': function.__code__ = replacement.__code__
                        return str.__eq__(self, other)
                class Mapping:
                    def keys(self):
                        trace.append('keys')
                        function.__code__ = replacement.__code__
                        return ['other']
                    def __getitem__(self, key): return value
                with function_attributes(function, __code__=function.__code__):
                    kwargs = Mapping() if phase == 'mapping' else {Key('other'): value}
                    if phase == 'before_render': function.__code__ = replacement.__code__
                    return run(engine, '{{ value is sameas(**kwargs) }}',
                               {'value': value, 'kwargs': kwargs}, trace)
            with self.subTest(phase=phase): self.check(execute)


if __name__ == '__main__': unittest.main()
