"""Observe the caller frame presented to an indent join override."""

import sys
import unittest
from collections.abc import Iterable
from typing import Any, Self
from unittest.mock import patch

import jinja2
from jinja2 import filters
from markupsafe import Markup
from test_native_filter_evaluation import render
from test_native_indent_filter import no_python_indent


class IndentJoinFrameTests(unittest.TestCase):
    def test_join_observes_filter_caller_frame(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[tuple[bool, Any, Any]]]:
            events: list[tuple[bool, Any, Any]] = []

            class CustomMarkup(Markup):
                def join(self, iterable: Iterable[Any]) -> Self:
                    frame = sys._getframe(1)
                    local = frame.f_locals
                    events.append((frame.f_code is filters.do_indent.__code__, local.get('rv'), local.get('lines')))
                    return type(self)(super().join(iterable))

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                result = render('{{value|indent(2,false,false)}}', {'value': CustomMarkup('first\nlast')},
                                native, environment)
            return result, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, ('first\n  last', [(True, 'first', ['last'])]))
                with no_python_indent():
                    actual = run(True, asynchronous)
                self.assertEqual(actual, expected)
