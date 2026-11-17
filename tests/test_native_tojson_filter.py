"""Differential and execution-boundary checks for Jinja's tojson filter."""

import gc
import sys
import unittest
from types import FrameType
from typing import Any
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
import jinja2.filters
import jinja2.utils
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate
from test_native_filter_evaluation import render


class NativeTojsonTests(unittest.TestCase):
    def test_serializer_traceback_frames_retain_locals_and_parent(self) -> None:
        def run(native: bool, asynchronous: bool) -> list[tuple[str, int, int, tuple[str, ...], str | None]]:
            environment = jinja2.Environment(enable_async=asynchronous)

            def dumps(value: Any, **kwargs: Any) -> str:
                raise ValueError('serializer failed')

            environment.policies['json.dumps_function'] = dumps
            try:
                render('{{ value|tojson }}', {'value': [1]}, native, environment)
            except ValueError as error:
                frames: list[tuple[str, int, int, tuple[str, ...], str | None]] = []
                traceback = error.__traceback__
                while traceback is not None:
                    frame = traceback.tb_frame
                    if frame.f_code in (jinja2.filters.do_tojson.__code__, jinja2.utils.htmlsafe_json_dumps.__code__):
                        parent = frame.f_back.f_code.co_name if frame.f_code is jinja2.utils.htmlsafe_json_dumps.__code__ and frame.f_back else None
                        frames.append((frame.f_code.co_name, traceback.tb_lineno, traceback.tb_lasti,
                                       tuple(sorted(frame.f_locals)), parent))
                    traceback = traceback.tb_next
                return frames
            raise AssertionError('expected serializer error')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(len(expected), 2)
                self.assertEqual(expected[-1][-1], 'do_tojson')
                self.assertEqual(run(True, asynchronous), expected)

    def test_serializer_error_preserves_jinja_traceback_frames(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[bool, list[str]]:
            failure = ValueError('serializer failed')
            environment = jinja2.Environment(enable_async=asynchronous)

            def dumps(value: Any, **kwargs: Any) -> str:
                raise failure

            environment.policies['json.dumps_function'] = dumps
            try:
                render('{{ value|tojson }}', {'value': [1]}, native, environment)
            except ValueError as error:
                frames: list[str] = []
                traceback = error.__traceback__
                while traceback is not None:
                    code = traceback.tb_frame.f_code
                    if code in (jinja2.filters.do_tojson.__code__, jinja2.utils.htmlsafe_json_dumps.__code__):
                        frames.append(code.co_name)
                    traceback = traceback.tb_next
                return error is failure, frames
            raise AssertionError('expected serializer error')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, (True, ['do_tojson', 'htmlsafe_json_dumps']))
                self.assertEqual(run(True, asynchronous), expected)

    def test_keyword_lifetimes_during_serializer_result_replacements(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[tuple[str, int]]]:
            events: list[tuple[str, int]] = []
            marker = object()

            class Result:
                def replace(self, old: str, new: str) -> 'Result':
                    events.append((old, sys.getrefcount(marker)))
                    return self

                def __str__(self) -> str:
                    return 'ok'

            def dumps(value: Any, **kwargs: Any) -> Result:
                return Result()

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.policies['json.dumps_function'] = dumps
            environment.policies['json.dumps_kwargs'] = {'default': marker}
            return render('{{ value|tojson }}', {'value': [1]}, native, environment), events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected[0], 'ok')
                self.assertEqual(run(True, asynchronous), expected)

    def test_serializer_observes_keyword_containers(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[tuple[int, list[tuple[str, ...]]]]]:
            events: list[tuple[int, list[tuple[str, ...]]]] = []

            class Marker:
                pass

            marker = Marker()
            options = {'default': marker, 'custom': marker}

            def dumps(value: Any, **kwargs: Any) -> str:
                count = sys.getrefcount(marker)
                containers = sorted(tuple(sorted(obj)) for obj in gc.get_referrers(marker)
                                    if isinstance(obj, dict) and obj.get('default') is marker)
                events.append((count, containers))
                return '"ok"'

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.policies['json.dumps_function'] = dumps
            environment.policies['json.dumps_kwargs'] = options
            output = render('{{ value|tojson }}', {'value': [1]}, native, environment)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(run(True, asynchronous), run(False, asynchronous))

    def test_serializer_observes_reference_counts(self) -> None:
        def run(native: bool, asynchronous: bool, profiled: bool) -> tuple[str, list[tuple[int, int]]]:
            events: list[tuple[int, int]] = []
            marker = object()
            options = {'default': marker}

            def dumps(value: Any, **kwargs: Any) -> str:
                events.append((sys.getrefcount(options), sys.getrefcount(kwargs['default'])))
                return '"ok"'

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.policies['json.dumps_function'] = dumps
            environment.policies['json.dumps_kwargs'] = options
            previous = sys.getprofile()
            try:
                if profiled:
                    sys.setprofile(lambda frame, event, arg: None)
                output = render('{{ value|tojson }}', {'value': [1]}, native, environment)
            finally:
                sys.setprofile(previous)
            return output, events

        for asynchronous in (False, True):
            for profiled in (False, True):
                with self.subTest(asynchronous=asynchronous, profiled=profiled):
                    self.assertEqual(run(True, asynchronous, profiled), run(False, asynchronous, profiled))

    def test_replaced_helper_is_called_with_live_policies(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[object]]:
            events: list[object] = []
            environment = jinja2.Environment(enable_async=asynchronous)

            def helper(value: Any, **kwargs: Any) -> str:
                events.append((value, kwargs['dumps'] is dumps, kwargs['sort_keys'], kwargs['indent']))
                return 'custom helper'

            def dumps(value: Any, **kwargs: Any) -> str:
                raise AssertionError('replacement helper owns dispatch')

            environment.policies['json.dumps_function'] = dumps
            environment.policies['json.dumps_kwargs'] = {'sort_keys': False}
            with patch.dict(jinja2.filters.__dict__, {'htmlsafe_json_dumps': helper}):
                output = render('{{ value|tojson(indent=2) }}', {'value': [1]}, native, environment)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(run(True, asynchronous), run(False, asynchronous))

    def test_invalid_policy_keywords_preserve_exception(self) -> None:
        def run(native: bool, asynchronous: bool, options: dict[Any, Any]) -> tuple[str, str]:
            environment = jinja2.Environment(enable_async=asynchronous)
            environment.policies['json.dumps_kwargs'] = options
            try:
                render('{{ value|tojson }}', {'value': [1]}, native, environment)
            except TypeError as error:
                return type(error).__name__, str(error)
            raise AssertionError('expected invalid keyword error')

        for asynchronous in (False, True):
            for options in ({'obj': 1}, {'dumps': 1}, {1: 2}):
                with self.subTest(asynchronous=asynchronous, options=options):
                    self.assertEqual(run(True, asynchronous, options), run(False, asynchronous, options))

    def test_custom_serializer_and_jinja_bodies(self) -> None:
        def run(native: bool, asynchronous: bool, document: bool) -> tuple[Any, list[object], list[str]]:
            events: list[object] = []
            calls: list[str] = []
            forbidden = {jinja2.filters.do_tojson.__code__, jinja2.utils.htmlsafe_json_dumps.__code__}

            class Policies(dict[str, Any]):
                def __getitem__(self, key: str) -> Any:
                    events.append(('policy', key))
                    return super().__getitem__(key)

            def dumps(value: Any, **kwargs: Any) -> str:
                events.append(('dumps', value, kwargs))
                return '"<&>\'"'

            def profile(frame: FrameType, event: str, argument: object) -> None:
                if event == 'call' and frame.f_code in forbidden:
                    calls.append(frame.f_code.co_name)

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.policies = Policies(environment.policies)
            environment.policies['json.dumps_function'] = dumps
            environment.policies['json.dumps_kwargs'] = {'sort_keys': False}
            source = '{{ value|tojson(indent=2) }}'
            values = {'value': {'a': 1}}
            previous = sys.getprofile()
            try:
                sys.setprofile(profile)
                result = (generate(docxtpl_native if native else docxtpl, source, values, environment)
                          if document else render(source, values, native, environment))
            finally:
                sys.setprofile(previous)
            return result, events, calls

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference, expected_events, reference_calls = run(False, asynchronous, False)
                candidate, actual_events, candidate_calls = run(True, asynchronous, False)
                self.assertEqual(reference, '"\\u003c\\u0026\\u003e\\u0027"')
                self.assertEqual(candidate, reference)
                self.assertEqual(actual_events, expected_events)
                self.assertEqual(reference_calls, ['do_tojson', 'htmlsafe_json_dumps'])
                self.assertEqual(candidate_calls, [])
                reference_doc, reference_events, _ = run(False, asynchronous, True)
                candidate_doc, candidate_events, candidate_calls = run(True, asynchronous, True)
                self.assertEqual(candidate_events, reference_events)
                self.assertEqual(candidate_calls, [])
                self.assertTrue(compare_docx(reference_doc, candidate_doc)['equivalent'])
