"""Exercise callback argument access through public and low-level routes."""

import io
import json
import unittest
from typing import Any

import docxtpl
import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template


class ArgumentTimingTests(unittest.TestCase):
    def test_callback_argument_class_is_not_probed_before_callback(self) -> None:
        def run(native: bool, asynchronous: bool, public: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []

            class Value:
                def __getattribute__(self, name: str) -> Any:
                    if name == '__class__':
                        events.append('class')
                        raise ValueError('unexpected class lookup')
                    return object.__getattribute__(self, name)

            value = Value()

            def callback(argument: object) -> str:
                events.append('callback:' + str(argument is value))
                return 'ok'

            context: dict[str, Any] = {'value': value, 'callback': callback}
            source = template('{{ callback(value) }}')
            environment = jinja2.Environment(enable_async=asynchronous)
            if public:
                child = object.__new__(docxtpl_native.Subdoc)
                child.tpl = None
                child.source = io.BytesIO(template('unused'))
                context['child'] = child
                engine = docxtpl_native if native else docxtpl
                return generate(engine, '', context, environment, source), events
            if not native:
                return generate(docxtpl, '', context, environment, source), events
            payload = json.dumps({
                'specials': [],
                'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
            })
            output, _ = docxtpl_native._extension().process_python_docx(
                source, context, BridgeConfig(environment, False), payload, False,
            )
            return bytes(output), events

        for asynchronous in (False, True):
            for public in (False, True):
                with self.subTest(asynchronous=asynchronous, public=public):
                    expected, events = run(False, asynchronous, public)
                    self.assertEqual(events, ['callback:True'])
                    actual, actual_events = run(True, asynchronous, public)
                    self.assertEqual(actual_events, events)
                    self.assertTrue(compare_docx(expected, actual)['equivalent'])


if __name__ == '__main__':
    unittest.main()
