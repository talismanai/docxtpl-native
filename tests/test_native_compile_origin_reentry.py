"""Differential checks for reentry while selecting template builtins."""

import builtins
import gc
import sys
import types
import unittest
import weakref
from typing import Any
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2.ext import Extension
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template


class CompilationOriginReentryTests(unittest.TestCase):
    def test_unicode_subclass_runtime_name_uses_module_registry_fallback(self) -> None:
        def observe(native: bool, asynchronous: bool, null_entry: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            original_import = builtins.__import__

            class Name(str):
                def __repr__(self) -> str:
                    events.append('repr:name')
                    return super().__repr__()

            class Target:
                @property
                def __spec__(self) -> Any:
                    events.append('fallback:spec')
                    return types.SimpleNamespace(_initializing=False)

            class RuntimeProxy:
                def __getattr__(self, name: str) -> Any:
                    if name == 'LoopContext':
                        events.append('missing')
                        raise AttributeError(name)
                    if name == '__name__':
                        return Name('custom.runtime')
                    return getattr(jinja2.runtime, name)

            def importing(name: str, globals: Any = None, locals: Any = None,
                          fromlist: Any = (), level: int = 0) -> Any:
                module = original_import(name, globals, locals, fromlist, level)
                if name == 'jinja2.runtime' and 'LoopContext' in fromlist:
                    return RuntimeProxy()
                return module

            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            with (patch.object(builtins, '__import__', importing),
                  patch.dict(sys.modules, {'custom.runtime.LoopContext': None if null_entry else Target()})):
                result = (session.render('case', '{{ value }}') if native
                          else environment.from_string('{{ value }}').render())
            return result, events

        for asynchronous in (False, True):
            for null_entry in (False, True):
                with self.subTest(asynchronous=asynchronous, null_entry=null_entry):
                    expected = ('', ['missing'] if null_entry else ['missing', 'fallback:spec'])
                    self.assertEqual(observe(False, asynchronous, null_entry), expected)
                    self.assertEqual(observe(True, asynchronous, null_entry), expected)

    def test_missing_nonstring_runtime_name_does_not_use_unknown_registry_key(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, object]:
            original_import = builtins.__import__

            class RuntimeProxy:
                def __getattr__(self, name: str) -> Any:
                    if name == 'LoopContext':
                        raise AttributeError(name)
                    if name == '__name__':
                        return 123
                    return getattr(jinja2.runtime, name)

            def importing(name: str, globals: Any = None, locals: Any = None,
                          fromlist: Any = (), level: int = 0) -> Any:
                module = original_import(name, globals, locals, fromlist, level)
                if name == 'jinja2.runtime' and 'LoopContext' in fromlist:
                    return RuntimeProxy()
                return module

            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            with (patch.object(builtins, '__import__', importing),
                  patch.dict(sys.modules, {'<unknown module name>.LoopContext': object()})):
                try:
                    if native:
                        session.render('case', '{{ value }}')
                    else:
                        environment.from_string('{{ value }}').render()
                except ImportError as error:
                    return str(error), error.name
            raise AssertionError('expected missing runtime import')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_missing_runtime_import_accepts_unicode_names_and_paths(self) -> None:
        def observe(native: bool, asynchronous: bool, mode: str) -> tuple[Any, ...]:
            events: list[str] = []
            original_import = builtins.__import__

            class Name(str):
                def __repr__(self) -> str:
                    events.append('repr:name')
                    return super().__repr__()

            module_name: Any = {'subclass': Name("name'quoted"), 'surrogate': 'name\ud800',
                                'nonstring': 123, 'raises': 'placeholder'}[mode]

            class RuntimeModule(types.ModuleType):
                def __getattribute__(self, name: str) -> Any:
                    if name == '__name__' and mode == 'raises':
                        events.append('name failed')
                        raise ValueError('name failed')
                    if name == 'LoopContext':
                        raise AttributeError(name)
                    return super().__getattribute__(name)

                def __getattr__(self, name: str) -> Any:
                    if name == 'LoopContext':
                        raise AttributeError(name)
                    return getattr(jinja2.runtime, name)

            def importing(name: str, globals: Any = None, locals: Any = None,
                          fromlist: Any = (), level: int = 0) -> Any:
                module = original_import(name, globals, locals, fromlist, level)
                if name == 'jinja2.runtime' and 'LoopContext' in fromlist:
                    proxy = RuntimeModule('placeholder')
                    proxy.__dict__['__name__'] = module_name
                    proxy.__dict__['__file__'] = 'path\ud800'
                    proxy.__dict__['__spec__'] = None
                    return proxy
                return module

            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            with patch.object(builtins, '__import__', importing):
                try:
                    if native:
                        session.render('case', '{{ value }}')
                    else:
                        environment.from_string('{{ value }}').render()
                except Exception as error:  # noqa: BLE001 - compare original exception and protocols
                    return type(error).__name__, error.args, getattr(error, 'name', None), \
                        getattr(error, 'path', None), events.copy()
            raise AssertionError('expected missing runtime import')

        for asynchronous in (False, True):
            for mode in ('subclass', 'surrogate', 'nonstring', 'raises'):
                with self.subTest(asynchronous=asynchronous, mode=mode):
                    self.assertEqual(observe(True, asynchronous, mode), observe(False, asynchronous, mode))

    def test_missing_runtime_import_spec_initializing_failure(self) -> None:
        def observe(native: bool, asynchronous: bool, during_bool: bool) -> tuple[str, str, bool, list[str]]:
            events: list[str] = []
            marker = ValueError('spec failed')
            original_import = builtins.__import__

            class Truth:
                def __bool__(self) -> bool:
                    events.append('bool')
                    raise marker

            class Spec:
                @property
                def _initializing(self) -> Any:
                    events.append('initializing')
                    if during_bool:
                        return Truth()
                    raise marker

            class RuntimeModule(types.ModuleType):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'LoopContext':
                        raise AttributeError(name)
                    return super().__getattribute__(name)

                def __getattr__(self, name: str) -> Any:
                    if name == 'LoopContext':
                        raise AttributeError(name)
                    return getattr(jinja2.runtime, name)

            def importing(name: str, globals: Any = None, locals: Any = None,
                          fromlist: Any = (), level: int = 0) -> Any:
                module = original_import(name, globals, locals, fromlist, level)
                if name == 'jinja2.runtime' and 'LoopContext' in fromlist:
                    proxy = RuntimeModule('jinja2.runtime')
                    proxy.__dict__['__file__'] = 'dictionary-path'
                    proxy.__dict__['__spec__'] = Spec()
                    return proxy
                return module

            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            with patch.object(builtins, '__import__', importing):
                try:
                    if native:
                        session.render('case', '{{ value }}')
                    else:
                        environment.from_string('{{ value }}').render()
                except Exception as error:  # noqa: BLE001 - compare the reference exception
                    return type(error).__name__, str(error), error.__context__ is marker, events.copy()
            raise AssertionError('expected missing runtime import')

        for asynchronous in (False, True):
            for during_bool in (False, True):
                with self.subTest(asynchronous=asynchronous, during_bool=during_bool):
                    self.assertEqual(observe(True, asynchronous, during_bool),
                                     observe(False, asynchronous, during_bool))

    def test_missing_runtime_import_uses_module_dictionary_and_spec(self) -> None:
        def observe(native: bool, asynchronous: bool, initializing: bool) -> tuple[str, object, object, list[str]]:
            events: list[str] = []
            original_import = builtins.__import__

            class RuntimeModule(types.ModuleType):
                def __getattribute__(self, name: str) -> Any:
                    if name in ('LoopContext', '__file__', '__spec__'):
                        events.append(name)
                    if name == 'LoopContext':
                        raise AttributeError(name)
                    if name == '__file__':
                        return 'overridden-path'
                    return super().__getattribute__(name)

                def __getattr__(self, name: str) -> Any:
                    if name == 'LoopContext':
                        raise AttributeError(name)
                    return getattr(jinja2.runtime, name)

            def importing(name: str, globals: Any = None, locals: Any = None,
                          fromlist: Any = (), level: int = 0) -> Any:
                module = original_import(name, globals, locals, fromlist, level)
                if name == 'jinja2.runtime' and 'LoopContext' in fromlist:
                    proxy = RuntimeModule('jinja2.runtime')
                    proxy.__dict__['__file__'] = 'dictionary-path'
                    proxy.__dict__['__spec__'] = types.SimpleNamespace(_initializing=initializing)
                    return proxy
                return module

            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            with patch.object(builtins, '__import__', importing):
                try:
                    if native:
                        session.render('case', '{{ value }}')
                    else:
                        environment.from_string('{{ value }}').render()
                except ImportError as error:
                    return str(error), error.name, error.path, events.copy()
            raise AssertionError('expected missing runtime import')

        for asynchronous in (False, True):
            for initializing in (False, True):
                with self.subTest(asynchronous=asynchronous, initializing=initializing):
                    expected = observe(False, asynchronous, initializing)
                    self.assertEqual(expected[2:], ('dictionary-path', ['LoopContext', '__spec__']))
                    self.assertEqual(observe(True, asynchronous, initializing), expected)

    def test_missing_runtime_import_releases_proxy_before_exception(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            original_import = builtins.__import__

            class RuntimeProxy:
                def __init__(self, module: Any) -> None:
                    self.module = module

                def __getattr__(self, name: str) -> Any:
                    if name == '__file__':
                        events.append('file lookup')
                    if name == 'LoopContext':
                        events.append('missing')
                        raise AttributeError(name)
                    return getattr(self.module, name)

                def __del__(self) -> None:
                    events.append('proxy released')

            def importing(name: str, globals: Any = None, locals: Any = None,
                          fromlist: Any = (), level: int = 0) -> Any:
                module = original_import(name, globals, locals, fromlist, level)
                if name == 'jinja2.runtime' and 'LoopContext' in fromlist:
                    return RuntimeProxy(module)
                return module

            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            with patch.object(builtins, '__import__', importing):
                try:
                    if native:
                        session.render('case', '{{ value }}')
                    else:
                        environment.from_string('{{ value }}').render()
                except ImportError as error:
                    return str(error), events.copy()
            raise AssertionError('expected missing runtime import')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                actual = observe(True, asynchronous)
                expected = observe(False, asynchronous)
                with self.subTest(part='lifetime'):
                    self.assertEqual(actual[1], expected[1])
                with self.subTest(part='message'):
                    self.assertEqual(actual[0], expected[0])

    def test_runtime_import_proxy_releases_before_selected_method(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            original_import = builtins.__import__
            original_from_code = jinja2.Template.from_code.__func__

            class FromCode:
                def __get__(self, instance: Any, owner: Any) -> Any:
                    def wrapper(cls: Any, env: Any, code: Any, globals: Any, uptodate: Any) -> Any:
                        return original_from_code(cls, env, code, globals, uptodate)

                    weakref.finalize(wrapper, events.append, 'method released')
                    return types.MethodType(wrapper, owner)

            class RuntimeProxy:
                def __init__(self, module: Any) -> None:
                    self.module = module

                def __getattr__(self, name: str) -> Any:
                    return getattr(self.module, name)

                def __del__(self) -> None:
                    events.append('proxy released')

            def importing(name: str, globals: Any = None, locals: Any = None,
                          fromlist: Any = (), level: int = 0) -> Any:
                module = original_import(name, globals, locals, fromlist, level)
                if name == 'jinja2.runtime' and 'LoopContext' in fromlist:
                    return RuntimeProxy(module)
                return module

            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            with patch.object(jinja2.Template, 'from_code', FromCode()), patch.object(builtins, '__import__', importing):
                result = (session.render('case', '{{ value }}') if native
                          else environment.from_string('{{ value }}').render())
            return result, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = ('', ['proxy released', 'method released'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_runtime_import_proxy_observes_reference_count(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[int]]:
            counts: list[int] = []
            original_import = builtins.__import__

            class RuntimeProxy:
                def __init__(self, module: Any) -> None:
                    self.module = module

                def __getattr__(self, name: str) -> Any:
                    if name == 'LoopContext':
                        counts.append(sys.getrefcount(self))
                    return getattr(self.module, name)

            def importing(name: str, globals: Any = None, locals: Any = None,
                          fromlist: Any = (), level: int = 0) -> Any:
                module = original_import(name, globals, locals, fromlist, level)
                if name == 'jinja2.runtime' and 'LoopContext' in fromlist:
                    return RuntimeProxy(module)
                return module

            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            with patch.object(builtins, '__import__', importing):
                result = (session.render('case', '{{ value }}') if native
                          else environment.from_string('{{ value }}').render())
            return result, counts

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_runtime_import_attribute_failure_retains_from_code_in_traceback(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[bool, list[str]]:
            events: list[str] = []
            marker = ValueError('runtime attribute failed')
            original_from_code = jinja2.Template.from_code.__func__
            original_import = builtins.__import__

            class FromCode:
                def __get__(self, instance: Any, owner: Any) -> Any:
                    def wrapper(cls: Any, env: Any, code: Any, globals: Any, uptodate: Any) -> Any:
                        return original_from_code(cls, env, code, globals, uptodate)

                    weakref.finalize(wrapper, events.append, 'released')
                    return types.MethodType(wrapper, owner)

            class RuntimeProxy:
                def __init__(self, module: Any) -> None:
                    self.module = module

                def __getattr__(self, name: str) -> Any:
                    if name == 'LoopContext':
                        events.append('attribute failed')
                        raise marker
                    return getattr(self.module, name)

            def importing(name: str, globals: Any = None, locals: Any = None,
                          fromlist: Any = (), level: int = 0) -> Any:
                module = original_import(name, globals, locals, fromlist, level)
                if name == 'jinja2.runtime' and 'LoopContext' in fromlist:
                    return RuntimeProxy(module)
                return module

            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            with patch.object(jinja2.Template, 'from_code', FromCode()), patch.object(builtins, '__import__', importing):
                try:
                    if native:
                        session.render('case', '{{ value }}')
                    else:
                        environment.from_string('{{ value }}').render()
                except ValueError as error:
                    return error is marker, events.copy()
            raise AssertionError('expected runtime attribute error')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = (True, ['attribute failed'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_clearing_import_traceback_releases_from_code_with_reentry(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[bool, list[str], list[str]]:
            events: list[str] = []
            marker = ImportError('runtime import failed')
            original_from_code = jinja2.Template.from_code.__func__
            original_import = builtins.__import__
            fail_import = True
            reenter = True
            session: Any = None

            def released() -> None:
                nonlocal reenter
                events.append('released')
                if reenter:
                    reenter = False
                    result = session.render('inner', 'INNER') if native else environment.from_string('INNER').render()
                    events.append('nested:' + result)

            class FromCode:
                def __get__(self, instance: Any, owner: Any) -> Any:
                    def wrapper(cls: Any, env: Any, code: Any, globals: Any, uptodate: Any) -> Any:
                        return original_from_code(cls, env, code, globals, uptodate)

                    weakref.finalize(wrapper, released)
                    return types.MethodType(wrapper, owner)

            def importing(name: str, globals: Any = None, locals: Any = None,
                          fromlist: Any = (), level: int = 0) -> Any:
                nonlocal fail_import
                if name == 'jinja2.runtime' and fail_import:
                    fail_import = False
                    events.append('import failed')
                    raise marker
                return original_import(name, globals, locals, fromlist, level)

            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            with patch.object(jinja2.Template, 'from_code', FromCode()), patch.object(builtins, '__import__', importing):
                try:
                    if native:
                        session.render('outer', '{{ value }}')
                    else:
                        environment.from_string('{{ value }}').render()
                except ImportError as error:
                    identical = error is marker
                    before = events.copy()
                else:
                    raise AssertionError('expected runtime import error')
                marker.__traceback__ = None
                gc.collect()
                return identical, before, events.copy()

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = (True, ['import failed'], ['import failed', 'released', 'released', 'nested:INNER'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_runtime_import_failure_retains_from_code_in_traceback(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[bool, list[str]]:
            events: list[str] = []
            marker = ImportError('runtime import failed')
            original_from_code = jinja2.Template.from_code.__func__
            original_import = builtins.__import__
            fail_import = True
            session: Any = None

            class FromCode:
                def __get__(self, instance: Any, owner: Any) -> Any:
                    def wrapper(cls: Any, env: Any, code: Any, globals: Any, uptodate: Any) -> Any:
                        return original_from_code(cls, env, code, globals, uptodate)

                    weakref.finalize(wrapper, events.append, 'released')
                    return types.MethodType(wrapper, owner)

            def importing(name: str, globals: Any = None, locals: Any = None,
                          fromlist: Any = (), level: int = 0) -> Any:
                nonlocal fail_import
                if name == 'jinja2.runtime' and fail_import:
                    fail_import = False
                    events.append('import failed')
                    raise marker
                return original_import(name, globals, locals, fromlist, level)

            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                session = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'value': 'OUTER'}
                )
            with patch.object(jinja2.Template, 'from_code', FromCode()), patch.object(builtins, '__import__', importing):
                try:
                    if native:
                        session.render('outer', '{{ value }}')
                    else:
                        environment.from_string('{{ value }}').render(value='OUTER')
                except ImportError as error:
                    return error is marker, events.copy()
            raise AssertionError('expected runtime import error')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = (True, ['import failed'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_selected_from_code_lifetime_when_preprocessing_raises(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[bool, list[str]]:
            events: list[str] = []
            marker = ValueError('preprocess failed')
            original = jinja2.Template.from_code.__func__

            class FromCode:
                def __get__(self, instance: Any, owner: Any) -> Any:
                    def wrapper(cls: Any, environment: Any, code: Any, globals: Any, uptodate: Any) -> Any:
                        return original(cls, environment, code, globals, uptodate)

                    weakref.finalize(wrapper, events.append, 'released')
                    return types.MethodType(wrapper, owner)

            class Preprocess(Extension):
                def preprocess(self, source: str, name: str | None,
                               filename: str | None = None) -> str:
                    events.append('preprocess')
                    raise marker

            environment = jinja2.Environment(enable_async=asynchronous, extensions=[Preprocess])
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            with patch.object(jinja2.Template, 'from_code', FromCode()):
                try:
                    if native:
                        session.render('case', '{{ value }}')
                    else:
                        environment.from_string('{{ value }}').render()
                except ValueError as error:
                    return error is marker, events
            raise AssertionError('expected preprocessing error')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = (True, ['preprocess', 'released'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_selected_custom_from_code_callback_is_invoked(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            original = jinja2.Template.from_code.__func__

            def wrapper(cls: Any, environment: Any, code: Any, globals: Any, uptodate: Any) -> Any:
                events.append('called')
                return original(cls, environment, code, globals, uptodate)

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.object(jinja2.Template, 'from_code', classmethod(wrapper)):
                if native:
                    result = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', 'text')
                else:
                    result = environment.from_string('text').render()
            return result, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = ('text', ['called'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_from_code_lifetime_during_complete_document(self) -> None:
        source = '{{ value }}'
        document = template(source)

        def observe(engine: Any, asynchronous: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []
            original = jinja2.Template.from_code.__func__

            class FromCode:
                def __get__(self, instance: Any, owner: Any) -> Any:
                    def wrapper(cls: Any, environment: Any, code: Any, globals: Any, uptodate: Any) -> Any:
                        return original(cls, environment, code, globals, uptodate)

                    weakref.finalize(wrapper, events.append, 'released')
                    return types.MethodType(wrapper, owner)

            class Preprocess(Extension):
                def preprocess(self, source: str, name: str | None,
                               filename: str | None = None) -> str:
                    events.append('preprocess')
                    return source

            environment = jinja2.Environment(enable_async=asynchronous, extensions=[Preprocess])
            with patch.object(jinja2.Template, 'from_code', FromCode()):
                output = generate(engine, source, {'value': 'OK'}, environment, source=document)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference, expected_events = observe(docxtpl, asynchronous)
                native, actual_events = observe(docxtpl_native, asynchronous)
                self.assertTrue(expected_events)
                self.assertEqual(actual_events, expected_events)
                self.assertTrue(compare_docx(reference, native)['equivalent'])

    def test_released_from_code_can_reenter_same_session(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            reenter = True
            session: Any = None
            original = jinja2.Template.from_code.__func__

            def released() -> None:
                nonlocal reenter
                events.append('released')
                if reenter:
                    reenter = False
                    result = session.render('inner', 'INNER') if native else environment.from_string('INNER').render()
                    events.append('nested:' + result)

            class FromCode:
                def __get__(self, instance: Any, owner: Any) -> Any:
                    def wrapper(cls: Any, env: Any, code: Any, globals: Any, uptodate: Any) -> Any:
                        return original(cls, env, code, globals, uptodate)

                    weakref.finalize(wrapper, released)
                    return types.MethodType(wrapper, owner)

            class Preprocess(Extension):
                def preprocess(self, source: str, name: str | None,
                               filename: str | None = None) -> str:
                    events.append('inner' if source == 'INNER' else 'outer')
                    return source

            environment = jinja2.Environment(enable_async=asynchronous, extensions=[Preprocess])
            if native:
                session = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'value': 'OK'}
                )
            with patch.object(jinja2.Template, 'from_code', FromCode()):
                result = session.render('outer', '{{ value }}') if native else environment.from_string(
                    '{{ value }}'
                ).render(value='OK')
            return result, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = ('OK', ['outer', 'released', 'inner', 'released', 'nested:INNER'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_selected_from_code_stays_alive_during_preprocessing(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            original = jinja2.Template.from_code.__func__

            class FromCode:
                def __get__(self, instance: Any, owner: Any) -> Any:
                    def wrapper(cls: Any, environment: Any, code: Any, globals: Any, uptodate: Any) -> Any:
                        return original(cls, environment, code, globals, uptodate)

                    weakref.finalize(wrapper, events.append, 'released')
                    return types.MethodType(wrapper, owner)

            class Preprocess(Extension):
                def preprocess(self, source: str, name: str | None,
                               filename: str | None = None) -> str:
                    events.append('preprocess')
                    return source

            environment = jinja2.Environment(enable_async=asynchronous, extensions=[Preprocess])
            with patch.object(jinja2.Template, 'from_code', FromCode()):
                if native:
                    session = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'value': 'OK'}
                    )
                    events.clear()
                    result = session.render('case', '{{ value }}')
                else:
                    events.clear()
                    result = environment.from_string('{{ value }}').render(value='OK')
            return result, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = ('OK', ['preprocess', 'released'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_failed_preprocessing_does_not_poison_next_compilation(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[bool, str, list[str]]:
            events: list[str] = []
            marker = ValueError('preprocess failed')
            first = True

            class Environment(jinja2.Environment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'template_class':
                        events.append('class')
                    return super().__getattribute__(name)

            class Preprocess(Extension):
                def preprocess(self, source: str, name: str | None,
                               filename: str | None = None) -> str:
                    nonlocal first
                    events.append('preprocess')
                    if first:
                        first = False
                        raise marker
                    return source

            environment = Environment(enable_async=asynchronous, extensions=[Preprocess])
            source = '{{ value }}'
            session = (docxtpl_native._extension().PythonRenderSession(
                BridgeConfig(environment, False), {'value': 'OK'}
            ) if native else None)
            events.clear()
            try:
                if session is not None:
                    session.render('case', source)
                else:
                    environment.from_string(source).render(value='OK')
            except ValueError as error:
                identical = error is marker
            else:
                raise AssertionError('expected preprocessing error')
            output = (session.render('case', source) if session is not None
                      else environment.from_string(source).render(value='OK'))
            return identical, output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = (True, 'OK', ['class', 'preprocess', 'class', 'preprocess'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_nested_render_on_same_session_keeps_outer_plan(self) -> None:
        def observe(native: bool, asynchronous: bool, loaded: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            armed = False
            session: Any = None

            class Environment(jinja2.Environment):
                def __getattribute__(self, name: str) -> Any:
                    nonlocal armed
                    if name == 'template_class':
                        events.append('class')
                        if armed:
                            armed = False
                            inner = session.render('inner', 'INNER') if native else self.from_string('INNER').render()
                            events.append('nested:' + inner)
                    return super().__getattribute__(name)

            class Loader(jinja2.DictLoader):
                def get_source(self, environment: Any, template: str) -> Any:
                    nonlocal armed
                    events.append('load')
                    armed = True
                    return super().get_source(environment, template)

            class Preprocess(Extension):
                def preprocess(self, source: str, name: str | None,
                               filename: str | None = None) -> str:
                    events.append('inner' if source == 'INNER' else 'child' if source == 'CHILD' else 'root')
                    return source

            environment = Environment(enable_async=asynchronous, loader=Loader({'child': 'CHILD'}),
                                      extensions=[Preprocess])
            source = '{% include "child" %}' if loaded else '{{ value }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'value': 'ROOT'}
                )
                events.clear()
                armed = not loaded
                result = session.render('case', source)
            else:
                events.clear()
                armed = not loaded
                result = environment.from_string(source).render(value='ROOT')
            return result, events

        for asynchronous, loaded in ((False, False), (False, True), (True, False)):
            with self.subTest(asynchronous=asynchronous, loaded=loaded):
                self.assertEqual(observe(True, asynchronous, loaded), observe(False, asynchronous, loaded))

    def test_nested_direct_render_from_template_class_lookup(self) -> None:
        def observe(native: bool, asynchronous: bool, loaded: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            armed = False
            inner_environment = jinja2.Environment()
            inner_session = (docxtpl_native._extension().PythonRenderSession(
                BridgeConfig(inner_environment, False), {}
            ) if native else None)

            def nested() -> str:
                if inner_session is not None:
                    return inner_session.render('inner', 'INNER')
                return inner_environment.from_string('INNER').render()

            class Environment(jinja2.Environment):
                def __getattribute__(self, name: str) -> Any:
                    nonlocal armed
                    if name == 'template_class':
                        events.append('class')
                        if armed:
                            armed = False
                            events.append('nested:' + nested())
                    return super().__getattribute__(name)

            class Loader(jinja2.DictLoader):
                def get_source(self, environment: Any, template: str) -> Any:
                    nonlocal armed
                    events.append('load')
                    armed = True
                    return super().get_source(environment, template)

            class Preprocess(Extension):
                def preprocess(self, source: str, name: str | None,
                               filename: str | None = None) -> str:
                    events.append('child' if source == 'CHILD' else 'root')
                    return source

            environment = Environment(enable_async=asynchronous, loader=Loader({'child': 'CHILD'}),
                                      extensions=[Preprocess])
            source = '{% include "child" %}' if loaded else '{{ value }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'value': 'ROOT'}
                )
                events.clear()
                armed = not loaded
                result = session.render('case', source)
            else:
                events.clear()
                armed = not loaded
                result = environment.from_string(source).render(value='ROOT')
            return result, events

        for asynchronous in (False, True):
            for loaded in (False, True):
                with self.subTest(asynchronous=asynchronous, loaded=loaded):
                    self.assertEqual(observe(True, asynchronous, loaded), observe(False, asynchronous, loaded))
