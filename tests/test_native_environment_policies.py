"""Differential tests for live Jinja policy mappings at the binding boundary."""

import unittest
from typing import Any

import docxtpl
import docxtpl_native
import jinja2
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate
from test_native_filter_evaluation import render


class EnvironmentPoliciesTests(unittest.TestCase):
    def test_initial_policies_for_builtin_filters(self) -> None:
        cases = [
            ('{{ value|truncate(8) }}', {'value': 'foo bar baz'}, {'truncate.leeway': 0}),
            ('{{ "foo bar baz"|truncate(8) }}', {}, {'truncate.leeway': 0}),
            ('{{ value|urlize }}', {'value': 'https://example.com'},
             {'urlize.rel': 'nofollow', 'urlize.target': '_blank'}),
            ('{{ value|urlize }}', {'value': 'git://example.com'},
             {'urlize.extra_schemes': ['git://']}),
            ('{{ value|tojson }}', {'value': {'z': 1, 'a': 2}},
             {'json.dumps_kwargs': {'sort_keys': False, 'indent': 2}}),
        ]
        for source, values, policies in cases:
            with self.subTest(source=source, policies=policies):
                reference = jinja2.Environment()
                candidate = jinja2.Environment()
                reference.policies.update(policies)
                candidate.policies.update(policies)
                self.assertEqual(render(source, values, True, candidate),
                                 render(source, values, False, reference))

    def test_mapping_access_and_missing_policy_errors(self) -> None:
        def run(native: bool, source: str, remove: bool) -> tuple[Any, list[object]]:
            events: list[object] = []

            class Policies(dict[str, Any]):
                def __getitem__(self, key: str) -> Any:
                    events.append(('getitem', key))
                    return super().__getitem__(key)

                def __eq__(self, other: object) -> bool:
                    events.append(('eq', other))
                    return super().__eq__(other)

                def __ne__(self, other: object) -> bool:
                    events.append(('ne', other))
                    return super().__ne__(other)

            environment = jinja2.Environment()
            environment.policies = Policies(environment.policies)
            environment.policies['truncate.leeway'] = 0
            if remove:
                del environment.policies['truncate.leeway']
            try:
                result: Any = ('result', render(source, {'value': 'foo bar baz'}, native, environment))
            except (KeyError, docxtpl_native.UnsupportedFeature) as error:
                result = ('error', type(error).__name__, str(error))
            return result, events

        for source, remove in [('plain', False), ('{{ value|truncate(8) }}', False),
                               ('{{ value|truncate(8) }}', True)]:
            with self.subTest(source=source, remove=remove):
                self.assertEqual(run(True, source, remove), run(False, source, remove))

    def test_json_callback_and_complete_document(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[bytes, list[object]]:
            events: list[object] = []
            environment = jinja2.Environment(enable_async=asynchronous)

            def dumps(value: Any, **kwargs: Any) -> str:
                events.append((value, kwargs))
                return '"custom <value>"'

            environment.policies['json.dumps_function'] = dumps
            environment.policies['json.dumps_kwargs'] = {'indent': 3}
            engine = docxtpl_native if native else docxtpl
            output = generate(engine, '{{ value|tojson }} {{ text|truncate(8) }}',
                              {'value': [1, 2], 'text': 'foo bar baz'}, environment)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference, expected_events = run(False, asynchronous)
                candidate, actual_events = run(True, asynchronous)
                self.assertEqual(actual_events, expected_events)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_callback_replaces_policy_mapping_during_render(self) -> None:
        def run(native: bool) -> tuple[str, list[object]]:
            environment = jinja2.Environment()
            events: list[object] = []

            @jinja2.pass_environment
            def change(actual: jinja2.Environment) -> str:
                events.append(actual is environment)
                actual.policies = dict(actual.policies, **{'truncate.leeway': 0})
                return ''

            source = '{{ change() }}{{ value|truncate(8) }}'
            return render(source, {'change': change, 'value': 'foo bar baz'}, native, environment), events

        self.assertEqual(run(True), run(False))
