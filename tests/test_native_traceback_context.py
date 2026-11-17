"""Differential checks for template traceback frames and retained locals."""

import unittest
from types import TracebackType
from typing import Any

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig


class TracebackContextTests(unittest.TestCase):
    def test_callback_exception_contains_template_frame_and_lexical_value(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[bool, list[tuple[str, int, bool, bool]]]:
            original = object()
            replacement = object()
            failure = RuntimeError('callback failed')

            @jinja2.pass_context
            def mutate(context: Any) -> str:
                context.vars['value'] = replacement
                return ''

            def crash() -> str:
                raise failure

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{{mutate()}}\n{{crash()}}\n{{value}}'
            context = {'mutate': mutate, 'crash': crash, 'value': original}
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('traceback-context', source)
                else:
                    environment.from_string(source).render(context)
            except RuntimeError as error:
                frames: list[tuple[str, int, bool, bool]] = []
                traceback: TracebackType | None = error.__traceback__
                while traceback is not None:
                    frame = traceback.tb_frame
                    if frame.f_code.co_filename == '<template>':
                        frames.append((frame.f_code.co_name, traceback.tb_lineno,
                                       frame.f_locals.get('value') is original,
                                       frame.f_locals.get('value') is replacement))
                    traceback = traceback.tb_next
                return error is failure, frames
            raise AssertionError('expected callback failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, (True, [('top-level template code', 2, True, False)]))
                self.assertEqual(observe(True, asynchronous), expected)


if __name__ == '__main__':
    unittest.main()
