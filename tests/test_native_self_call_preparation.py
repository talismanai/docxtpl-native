"""Compare dispatch lookup timing for self-block calls."""

import unittest
from typing import Any, cast

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import runtime
from jinja2.sandbox import SandboxedEnvironment


class SelfCallPreparationTests(unittest.TestCase):
    def test_self_attribute_lookup_error_precedes_argument(self) -> None:
        def observe(native: bool, asynchronous: bool, sandboxed: bool) -> tuple[bool, list[str]]:
            events: list[str] = []
            original = ValueError('self attribute lookup failed')

            class AttributeLookup:
                def getattr(self, obj: Any, attribute: str) -> Any:
                    events.append('getattr:' + attribute)
                    if attribute == 'body':
                        raise original
                    return cast(Any, super()).getattr(obj, attribute)

            class Environment(AttributeLookup, jinja2.Environment):
                pass

            class Sandbox(AttributeLookup, SandboxedEnvironment):
                pass

            class Argument:
                @property
                def value(self) -> int:
                    events.append('argument')
                    return 7

            environment = Sandbox(enable_async=asynchronous) if sandboxed else Environment(enable_async=asynchronous)
            source = '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{self.body(argument.value)}}'
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'argument': Argument()}
                    ).render('case', source)
                else:
                    environment.from_string(source).render(argument=Argument())
            except ValueError as error:
                return error is original, events
            except (TypeError, docxtpl_native.UnsupportedFeature):
                return False, events
            raise AssertionError('expected a lookup failure')

        for asynchronous in (False, True):
            for sandboxed in (False, True):
                with self.subTest(asynchronous=asynchronous, sandboxed=sandboxed):
                    self.assertEqual(observe(False, asynchronous, sandboxed), (True, ['getattr:body']))
                    self.assertEqual(observe(True, asynchronous, sandboxed), (True, ['getattr:body']))

    def test_mapping_expansion_uses_captured_dispatcher(self) -> None:
        def observe(native: bool, asynchronous: bool, sandboxed: bool) -> list[str]:
            events: list[str] = []
            contexts: list[Any] = []

            class Context(runtime.Context):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    super().__init__(*args, **kwargs)
                    contexts.append(self)

                def __getattribute__(self, name: str) -> Any:
                    if name == 'call':
                        events.append('call lookup')
                    return super().__getattribute__(name)

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'call':
                        events.append('call lookup')
                    return super().__getattribute__(name)

            environment = Sandbox(enable_async=asynchronous) if sandboxed else jinja2.Environment(enable_async=asynchronous)
            if not sandboxed:
                environment.context_class = Context

            class Mapping:
                def keys(self) -> list[str]:
                    events.append('keys')
                    owner: Any = environment if sandboxed else contexts[-1]

                    def replacement(*args: Any, **kwargs: Any) -> str:
                        return 'replacement'

                    owner.call = replacement
                    return ['key']

                def __getitem__(self, key: str) -> int:
                    events.append('get:' + key)
                    return 7

            source = '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{self.body(**mapping)}}'
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'mapping': Mapping()}
                    ).render('case', source)
                else:
                    environment.from_string(source).render(mapping=Mapping())
            except (TypeError, docxtpl_native.UnsupportedFeature):
                return events
            raise AssertionError('expected a rejected call')

        for asynchronous in (False, True):
            for sandboxed in (False, True):
                with self.subTest(asynchronous=asynchronous, sandboxed=sandboxed):
                    self.assertEqual(observe(False, asynchronous, sandboxed), ['call lookup', 'keys', 'get:key'])
                    self.assertEqual(observe(True, asynchronous, sandboxed), ['call lookup', 'keys', 'get:key'])

    def test_dispatch_lookup_precedes_argument_access(self) -> None:
        def observe(native: bool, asynchronous: bool, sandboxed: bool) -> list[str]:
            events: list[str] = []

            class Context(runtime.Context):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'call':
                        events.append('call lookup')
                    return super().__getattribute__(name)

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'call':
                        events.append('call lookup')
                    return super().__getattribute__(name)

            class Argument:
                @property
                def value(self) -> int:
                    events.append('argument')
                    return 7

            environment = Sandbox(enable_async=asynchronous) if sandboxed else jinja2.Environment(enable_async=asynchronous)
            if not sandboxed:
                environment.context_class = Context
            source = '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{self.body(argument.value)}}'
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'argument': Argument()}
                    ).render('case', source)
                else:
                    environment.from_string(source).render(argument=Argument())
            except (TypeError, docxtpl_native.UnsupportedFeature):
                return events
            raise AssertionError('expected a rejected call')

        for asynchronous in (False, True):
            for sandboxed in (False, True):
                with self.subTest(asynchronous=asynchronous, sandboxed=sandboxed):
                    self.assertEqual(observe(False, asynchronous, sandboxed), ['call lookup', 'argument'])
                    self.assertEqual(observe(True, asynchronous, sandboxed), ['call lookup', 'argument'])

    def test_dispatch_lookup_error_precedes_argument(self) -> None:
        def observe(native: bool, asynchronous: bool, sandboxed: bool) -> tuple[bool, list[str]]:
            events: list[str] = []
            original = ValueError('call lookup failed')

            class Context(runtime.Context):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'call':
                        events.append('call lookup')
                        raise original
                    return super().__getattribute__(name)

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'call':
                        events.append('call lookup')
                        raise original
                    return super().__getattribute__(name)

            class Argument:
                @property
                def value(self) -> int:
                    events.append('argument')
                    return 7

            environment = Sandbox(enable_async=asynchronous) if sandboxed else jinja2.Environment(enable_async=asynchronous)
            if not sandboxed:
                environment.context_class = Context
            source = '{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{self.body(argument.value)}}'
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'argument': Argument()}
                    ).render('case', source)
                else:
                    environment.from_string(source).render(argument=Argument())
            except ValueError as error:
                return error is original, events
            except (docxtpl_native.UnsupportedFeature, TypeError):
                return False, events
            raise AssertionError('expected a lookup failure')

        for asynchronous in (False, True):
            for sandboxed in (False, True):
                with self.subTest(asynchronous=asynchronous, sandboxed=sandboxed):
                    self.assertEqual(observe(False, asynchronous, sandboxed), (True, ['call lookup']))
                    self.assertEqual(observe(True, asynchronous, sandboxed), (True, ['call lookup']))
