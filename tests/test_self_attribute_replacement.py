"""A custom environment may replace a self-block lookup with a callable."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from types import FrameType
from typing import Any
from unittest.mock import patch

import jinja2
import docxtpl
from docx import Document
from jinja2 import runtime
from jinja2.sandbox import SandboxedEnvironment

import docxtpl_native
from docxtpl_native.bridge import BridgeConfig
from paritylab.protocol_base import generate


class SelfAttributeReplacementTests(unittest.TestCase):
    def test_constructor_replaced_before_import_is_not_misclassified(self) -> None:
        script = '''
import json
import sys
import jinja2
from jinja2 import runtime
from typing import Any

events = []
runtime.__dict__['_self_constructor_events'] = events
source = "def __init__(self: object, context: object) -> None:\\n    _self_constructor_events.append('init')\\n    self._TemplateReference__context = context\\n"
namespace = runtime.__dict__
exec(compile(source, runtime.__file__, 'exec'), namespace)
runtime.TemplateReference.__init__ = namespace['__init__']

class Environment(jinja2.Environment):
    def getattr(self, obj: Any, attribute: str) -> Any:
        if type(obj) is runtime.TemplateReference and attribute == 'body':
            return lambda value: str(value)
        return super().getattr(obj, attribute)

environment = Environment()
source = '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{ self.body(1) }}'
if sys.argv[1] == 'native':
    import docxtpl_native
    from docxtpl_native.bridge import BridgeConfig
    output = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {}).render('case', source)
else:
    output = environment.from_string(source).render()
print(json.dumps([output, events]))
'''
        outcomes: list[Any] = []
        for mode in ('reference', 'native'):
            result = subprocess.run([sys.executable, '-c', script, mode], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            outcomes.append(json.loads(result.stdout))
        self.assertEqual(outcomes[0], ['1', ['init']])
        self.assertEqual(outcomes[1], outcomes[0])

    def test_include_keeps_distinct_self_and_restores_caller_identity(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, tuple[bool, bool]]:
            seen: list[Any] = []

            class Environment(jinja2.Environment):
                def getattr(self, obj: Any, attribute: str) -> Any:
                    if type(obj) is runtime.TemplateReference and attribute == 'body':
                        seen.append(obj)
                        return lambda value: str(value)
                    return super().getattr(obj, attribute)

            child = ('{% if false %}{% block body %}CHILD{% endblock %}{% endif %}'
                     '{{ self.body(2) }}')
            source = ('{% if false %}{% block body %}ROOT{% endblock %}{% endif %}'
                      '{{ self.body(1) }}|{% include "child" %}|{{ self.body(3) }}')
            environment = Environment(enable_async=asynchronous, loader=jinja2.DictLoader({'child': child}))
            try:
                if native:
                    result = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    result = environment.from_string(source).render()
            except Exception as error:
                return type(error).__name__, (False, False)
            return result, (len(seen) == 3 and seen[0] is seen[2],
                            len(seen) == 3 and seen[0] is not seen[1])

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('1|2|3', (True, True)))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_template_reference_class_is_captured_before_context_construction(self) -> None:
        original = runtime.TemplateReference

        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Replacement:
                def __init__(self, context: Any) -> None:
                    events.append('replacement constructor')

            class Context(runtime.Context):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    super().__init__(*args, **kwargs)
                    events.append('context')
                    runtime.TemplateReference = Replacement  # type: ignore[assignment]

            class Environment(jinja2.Environment):
                def getattr(self, obj: Any, attribute: str) -> Any:
                    if type(obj) is original and attribute == 'body':
                        events.append('original reference')
                        return lambda value: str(value)
                    return super().getattr(obj, attribute)

            environment = Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{ self.body(1) }}'
            try:
                if native:
                    result = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    result = environment.from_string(source).render()
            except Exception as error:
                result = type(error).__name__
            finally:
                runtime.TemplateReference = original
            return result, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('1', ['context', 'original reference']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_self_constructor_runs_before_root_even_when_call_is_skipped(self) -> None:
        original = runtime.TemplateReference.__init__

        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def initialize(reference: Any, context: Any) -> None:
                events.append('init')
                original(reference, context)

            def touch() -> str:
                events.append('root')
                return 'ok'

            environment = jinja2.Environment(enable_async=asynchronous)
            source = ('{% if false %}{{ self.body(1) }}{% endif %}'
                      '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{ touch() }}')
            with patch.object(runtime.TemplateReference, '__init__', initialize):
                if native:
                    result = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'touch': touch}
                    ).render('case', source)
                else:
                    result = environment.from_string(source).render(touch=touch)
            return result, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('ok', ['init', 'root']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_sandbox_checks_replacement_callable(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def replacement(value: int) -> str:
                events.append(f'call:{value}')
                return 'unsafe'

            class Sandbox(SandboxedEnvironment):
                def getattr(self, obj: Any, attribute: str) -> Any:
                    if type(obj) is runtime.TemplateReference and attribute == 'body':
                        events.append('lookup')
                        return replacement
                    return super().getattr(obj, attribute)

                def is_safe_callable(self, obj: Any) -> bool:
                    if obj is replacement:
                        events.append('policy')
                        return False
                    return super().is_safe_callable(obj)

            class Argument:
                @property
                def value(self) -> int:
                    events.append('argument')
                    return 7

            environment = Sandbox(enable_async=asynchronous)
            source = '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{ self.body(argument.value) }}'
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'argument': Argument()}
                    ).render('case', source)
                else:
                    environment.from_string(source).render(argument=Argument())
            except Exception as error:
                return type(error).__name__, events
            return 'unexpected success', events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('SecurityError', ['lookup', 'argument', 'policy']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_replaced_template_reference_constructor_remains_callback(self) -> None:
        original = runtime.TemplateReference.__init__

        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def initialize(reference: Any, context: Any) -> None:
                events.append('init')
                original(reference, context)

            class Environment(jinja2.Environment):
                def getattr(self, obj: Any, attribute: str) -> Any:
                    if type(obj) is runtime.TemplateReference and attribute == 'body':
                        return lambda value: str(value)
                    return super().getattr(obj, attribute)

            environment = Environment(enable_async=asynchronous)
            source = '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{ self.body(1) }}'
            try:
                with patch.object(runtime.TemplateReference, '__init__', initialize):
                    if native:
                        result = docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {}
                        ).render('case', source)
                    else:
                        result = environment.from_string(source).render()
            except Exception as error:
                return type(error).__name__, events
            return result, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('1', ['init']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_template_reference_constructor_runs_natively(self) -> None:
        constructor = runtime.TemplateReference.__init__.__code__

        def observe(native: bool) -> tuple[str, list[str]]:
            calls: list[str] = []

            class Environment(jinja2.Environment):
                def getattr(self, obj: Any, attribute: str) -> Any:
                    if type(obj) is runtime.TemplateReference and attribute == 'body':
                        return lambda value: str(value)
                    return super().getattr(obj, attribute)

            def profile(frame: FrameType, event: str, argument: object) -> None:
                if event == 'call' and frame.f_code is constructor:
                    calls.append('constructor')

            environment = Environment()
            source = '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{ self.body(1) }}'
            previous = sys.getprofile()
            try:
                sys.setprofile(profile)
                if native:
                    result = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    result = environment.from_string(source).render()
            except Exception as error:
                return type(error).__name__, calls
            finally:
                sys.setprofile(previous)
            return result, calls

        self.assertEqual(observe(False), ('1', ['constructor']))
        self.assertEqual(observe(True), ('1', []))

    def test_repeated_lookup_uses_same_template_reference(self) -> None:
        def observe(native: bool) -> tuple[str, list[bool]]:
            seen: list[Any] = []

            class Environment(jinja2.Environment):
                def getattr(self, obj: Any, attribute: str) -> Any:
                    if type(obj) is runtime.TemplateReference and attribute == 'body':
                        seen.append(obj)
                        return lambda value: str(value)
                    return super().getattr(obj, attribute)

            environment = Environment()
            source = ('{% if false %}{% block body %}BODY{% endblock %}{% endif %}'
                      '{{ self.body(1) }}|{{ self.body(2) }}')
            try:
                if native:
                    result = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    result = environment.from_string(source).render()
            except Exception as error:
                return type(error).__name__, [item is seen[0] for item in seen]
            return result, [item is seen[0] for item in seen]

        expected = observe(False)
        self.assertEqual(expected, ('1|2', [True, True]))
        self.assertEqual(observe(True), expected)

    def test_lookup_replacement_runs_with_evaluated_argument(self) -> None:
        def observe(native: bool, asynchronous: bool, sandboxed: bool,
                    document: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Lookup:
                def getattr(self, obj: Any, attribute: str) -> Any:
                    if type(obj) is runtime.TemplateReference and attribute == 'body':
                        events.append('lookup')

                        def replacement(value: int) -> str:
                            events.append(f'call:{value}')
                            return f'custom:{value}'

                        return replacement
                    return super().getattr(obj, attribute)  # type: ignore[misc]

            class Environment(Lookup, jinja2.Environment):
                pass

            class Sandbox(Lookup, SandboxedEnvironment):
                pass

            class Argument:
                @property
                def value(self) -> int:
                    events.append('argument')
                    return 7

            environment = Sandbox(enable_async=asynchronous) if sandboxed else Environment(enable_async=asynchronous)
            source = '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{ self.body(argument.value) }}'
            try:
                if document:
                    engine = docxtpl_native if native else docxtpl
                    blob = generate(engine, source, {'argument': Argument()}, environment)
                    output = Document(io.BytesIO(blob)).paragraphs[0].text
                elif native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'argument': Argument()}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(argument=Argument())
            except Exception as error:
                return type(error).__name__, events
            return output, events

        for asynchronous in (False, True):
            for sandboxed in (False, True):
                for document in (False, True):
                    with self.subTest(asynchronous=asynchronous, sandboxed=sandboxed, document=document):
                        expected = observe(False, asynchronous, sandboxed, document)
                        self.assertEqual(expected, ('custom:7', ['lookup', 'argument', 'call:7']))
                        self.assertEqual(observe(True, asynchronous, sandboxed, document), expected)


if __name__ == '__main__':
    unittest.main()
