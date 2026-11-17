"""The public join callback can observe and call the attribute getter."""

from __future__ import annotations

import builtins
import inspect
import sys
import unittest
from types import CodeType, FrameType
from typing import Any
from unittest.mock import patch

import jinja2
import jinja2.filters as filters
import docxtpl
import docxtpl_native

from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template
from test_native_filter_evaluation import render


class JoinGetterCallbackBoundaryTests(unittest.TestCase):
    def test_user_map_getter_retains_annotations_globals_and_current_builtins(self) -> None:
        factory = filters.make_attrgetter
        expected_annotations = factory(jinja2.Environment(), 'x').__annotations__

        def observe(native: bool) -> tuple[str, list[tuple[bool, bool, bool]]]:
            events: list[tuple[bool, bool, bool]] = []

            def custom_map(function: Any, values: Any) -> Any:
                if function is not str:
                    events.append((function.__annotations__ == expected_annotations,
                                   function.__globals__ is factory.__globals__,
                                   function.__builtins__ is filters.__dict__['__builtins__']))
                return builtins.map(function, values)

            with patch.dict(filters.__dict__, {'map': custom_map, '__builtins__': {}}):
                result = render('{{ values|join(",", attribute="x") }}',
                                {'values': [{'x': 'a'}, {'x': 'b'}]}, native)
            return result, events

        expected = observe(False)
        self.assertEqual(expected, ('a,b', [(True, True, True)]))
        self.assertEqual(observe(True), expected)

    def test_complete_document_uses_native_attribute_join(self) -> None:
        monitored = {filters.sync_do_join.__code__: 'sync_do_join',
                     filters.make_attrgetter.__code__: 'make_attrgetter'}
        source = template('{{ values|join(",", attribute="x") }}')

        def observe(native: bool, asynchronous: bool) -> tuple[bytes, list[str]]:
            calls: list[str] = []

            def profile(frame: FrameType, event: str, argument: object) -> None:
                if event == 'call' and frame.f_code in monitored:
                    calls.append(monitored[frame.f_code])

            previous = sys.getprofile()
            try:
                sys.setprofile(profile)
                output = generate(docxtpl_native if native else docxtpl, '',
                                  {'values': [{'x': 'a'}, {'x': 'b'}]},
                                  jinja2.Environment(enable_async=asynchronous), source=source)
            finally:
                sys.setprofile(previous)
            return output, calls

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference, reference_calls = observe(False, asynchronous)
                candidate, candidate_calls = observe(True, asynchronous)
                self.assertIn('sync_do_join', reference_calls)
                self.assertIn('make_attrgetter', reference_calls)
                comparison = compare_docx(reference, candidate)
                self.assertTrue(comparison['equivalent'], comparison)
                self.assertEqual(candidate_calls, [])

    def test_attribute_join_does_not_execute_standard_python_algorithms(self) -> None:
        monitored = {filters.sync_do_join.__code__: 'sync_do_join',
                     filters.make_attrgetter.__code__: 'make_attrgetter'}

        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            calls: list[str] = []

            def profile(frame: FrameType, event: str, argument: object) -> None:
                if event == 'call' and frame.f_code in monitored:
                    calls.append(monitored[frame.f_code])

            previous = sys.getprofile()
            try:
                sys.setprofile(profile)
                output = render('{{ values|join(",", attribute="x") }}',
                                {'values': [{'x': 'a'}, {'x': 'b'}]}, native,
                                jinja2.Environment(enable_async=asynchronous))
            finally:
                sys.setprofile(previous)
            return output, calls

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference = observe(False, asynchronous)
                self.assertEqual(reference[0], 'a,b')
                self.assertIn('sync_do_join', reference[1])
                self.assertIn('make_attrgetter', reference[1])
                self.assertEqual(observe(True, asynchronous), ('a,b', []))

    def test_user_map_observes_and_invokes_reference_getter(self) -> None:
        inner_code = next(value for value in filters.make_attrgetter.__code__.co_consts
                          if isinstance(value, CodeType) and value.co_name == 'attrgetter')

        def observe(native: bool) -> tuple[str, list[tuple[bool, bool, list[str], int]]]:
            events: list[tuple[bool, bool, list[str], int]] = []

            def custom_map(function: Any, values: Any) -> Any:
                if function is str:
                    return builtins.map(function, values)
                calls: list[str] = []

                def profile(frame: FrameType, event: str, argument: object) -> None:
                    if event == 'call' and frame.f_code is inner_code:
                        calls.append(event)

                previous = sys.getprofile()
                try:
                    sys.setprofile(profile)
                    mapped = [function(value) for value in values]
                finally:
                    sys.setprofile(previous)
                events.append((inspect.isfunction(function), getattr(function, '__code__', None) is inner_code,
                               mapped, len(calls)))
                return iter(mapped)

            with patch.dict(filters.__dict__, {'map': custom_map}):
                output = render('{{ values|join(",", attribute="x") }}',
                                {'values': [{'x': 'a'}, {'x': 'b'}]}, native)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('a,b', [(True, True, ['a', 'b'], 2)]))
        self.assertEqual(observe(True), expected)

    def test_user_join_observes_getter_inside_builtin_maps(self) -> None:
        inner_code = next(value for value in filters.make_attrgetter.__code__.co_consts
                          if isinstance(value, CodeType) and value.co_name == 'attrgetter')

        def observe(native: bool) -> tuple[str, list[tuple[bool, bool]]]:
            events: list[tuple[bool, bool]] = []

            class Delimiter:
                def join(self, values: Any) -> str:
                    outer = values.__reduce__()[1]
                    inner = outer[1].__reduce__()[1]
                    getter = inner[0]
                    events.append((inspect.isfunction(getter), getattr(getter, '__code__', None) is inner_code))
                    return ','.join(values)

            class String(str):
                def __new__(cls, value: object) -> Any:
                    if value == ',':
                        return Delimiter()
                    return builtins.str(value)

            with patch.dict(filters.__dict__, {'str': String}):
                output = render('{{ values|join(",", attribute="x") }}',
                                {'values': [{'x': 'a'}, {'x': 'b'}]}, native)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('a,b', [(True, True)]))
        self.assertEqual(observe(True), expected)


if __name__ == '__main__':
    unittest.main()
