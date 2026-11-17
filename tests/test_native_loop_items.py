"""Differential checks for item access on loop metadata and methods."""

import asyncio
import subprocess
import sys
import textwrap
import unittest
import weakref
from typing import Any

import docxtpl
import docxtpl_native
import jinja2
import jinja2.async_utils as async_utils
import jinja2.environment as environment_module
import jinja2.runtime as runtime_module
import jinja2.sandbox as sandbox_module
from docxtpl_native.bridge import BridgeConfig
from jinja2.runtime import LoopContext
from jinja2.sandbox import SandboxedEnvironment
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template


class LoopItemTests(unittest.TestCase):
    def test_reference_previous_item_finalizer_can_advance_same_loop(self) -> None:
        events: list[tuple[int, int]] = []
        loop: LoopContext

        class Item:
            def __del__(self) -> None:
                value, current = next(loop)
                events.append((value, current.index))

        def values() -> Any:
            yield Item()
            yield 2
            yield 3
            yield 4

        loop = LoopContext(values(), jinja2.Undefined)
        next(loop)
        next(loop)
        value, _ = next(loop)
        self.assertEqual((value, loop.index, loop.previtem, events), (3, 4, 2, [(4, 4)]))

    def test_previous_item_finalizer_runs_before_third_body(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Item:
                def __del__(self) -> None:
                    events.append('released')

            def values() -> Any:
                for number in (1, 2, 3):
                    events.append(f'yield:{number}')
                    yield Item() if number == 1 else number

            def record(index: int) -> str:
                events.append(f'body:{index}')
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% for item in values %}{{ record(loop.index) }}{% endfor %}'
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'values': values(), 'record': record}
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values=values(), record=record)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = ('', ['yield:1', 'body:1', 'yield:2', 'body:2',
                                 'yield:3', 'released', 'body:3'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_lookahead_rechecks_missing_after_iterator_changes_it(self) -> None:
        for item_access in (False, True):
            with self.subTest(item_access=item_access):
                def observe(native: bool) -> tuple[str, list[str]]:
                    events: list[str] = []
                    original = runtime_module.missing

                    def values() -> Any:
                        events.append('first')
                        yield 1
                        events.append('change')
                        runtime_module.missing = object()

                    access = "loop['last']" if item_access else 'loop.last'
                    source = '{% for x in values %}{{ ' + access + ' }}{% break %}{% endfor %}'
                    environment = jinja2.Environment(extensions=['jinja2.ext.loopcontrols'])
                    try:
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': values()}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=values())
                        return output, events
                    finally:
                        runtime_module.missing = original

                expected = observe(False)
                self.assertEqual(expected, ('False', ['first', 'change']))
                self.assertEqual(observe(True), expected)

    def test_async_lookahead_treats_yielded_missing_as_missing(self) -> None:
        for asynchronous_source in (False, True):
            for item_access in (False, True):
                with self.subTest(asynchronous_source=asynchronous_source, item_access=item_access):
                    def observe(native: bool) -> tuple[str, list[str]]:
                        events: list[str] = []

                        def values() -> Any:
                            events.append('first')
                            yield 1
                            events.append('missing')
                            yield runtime_module.missing

                        async def async_values() -> Any:
                            events.append('first')
                            yield 1
                            events.append('missing')
                            yield runtime_module.missing

                        access = "loop['last']" if item_access else 'loop.last'
                        source = '{% for x in values %}{{ ' + access + ' }}{% break %}{% endfor %}'
                        environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
                        iterator = async_values() if asynchronous_source else values()
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': iterator}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=iterator)
                        return output, events

                    expected = observe(False)
                    self.assertEqual(expected, ('True', ['first', 'missing']))
                    self.assertEqual(observe(True), expected)

    def test_lookahead_observes_missing_removed_by_iterator(self) -> None:
        def observe(native: bool) -> tuple[str, str, list[str]]:
            events: list[str] = []
            original = runtime_module.missing

            def values() -> Any:
                events.append('first')
                yield 1
                events.append('delete')
                del runtime_module.missing

            source = '{% for x in values %}{{ loop.last }}{% break %}{% endfor %}'
            environment = jinja2.Environment(extensions=['jinja2.ext.loopcontrols'])
            try:
                try:
                    if native:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {'values': values()}
                        ).render('case', source)
                    else:
                        environment.from_string(source).render(values=values())
                except Exception as error:  # noqa: BLE001 - compare the original runtime failure
                    return type(error).__name__, str(error), events
                raise AssertionError('expected missing global lookup to fail')
            finally:
                runtime_module.missing = original

        expected = observe(False)
        self.assertEqual(expected, ('NameError', "name 'missing' is not defined", ['first', 'delete']))
        self.assertEqual(observe(True), expected)

    def test_async_exhaustion_preserves_missing_error_context(self) -> None:
        for asynchronous_source in (False, True):
            with self.subTest(asynchronous_source=asynchronous_source):
                def observe(native: bool) -> tuple[str, str, str, str, list[str]]:
                    events: list[str] = []
                    original = runtime_module.missing

                    def values() -> Any:
                        events.append('first')
                        yield 1
                        events.append('delete')
                        del runtime_module.missing

                    async def async_values() -> Any:
                        events.append('first')
                        yield 1
                        events.append('delete')
                        del runtime_module.missing

                    source = '{% for x in values %}{{ loop.last }}{% break %}{% endfor %}'
                    environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
                    iterator = async_values() if asynchronous_source else values()
                    try:
                        try:
                            if native:
                                docxtpl_native._extension().PythonRenderSession(
                                    BridgeConfig(environment, False), {'values': iterator}
                                ).render('case', source)
                            else:
                                environment.from_string(source).render(values=iterator)
                        except Exception as error:  # noqa: BLE001 - compare implicit exception chaining
                            context = error.__context__
                            cause = context.__cause__ if context is not None else None
                            return type(error).__name__, str(error), type(context).__name__, type(cause).__name__, events
                        raise AssertionError('expected missing global lookup to fail')
                    finally:
                        runtime_module.missing = original

                self.assertEqual(observe(True), observe(False))

    def test_adapted_lookahead_preserves_stop_iteration_cause_identity(self) -> None:
        def observe(native: bool) -> tuple[bool, Any, list[str]]:
            events: list[str] = []
            original = runtime_module.missing
            failure = StopIteration('end value')

            class Items:
                def __iter__(self) -> Any:
                    return self

                def __next__(self) -> int:
                    if not events:
                        events.append('first')
                        return 1
                    events.append('delete')
                    del runtime_module.missing
                    raise failure

            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            source = '{% for x in values %}{{ loop.last }}{% break %}{% endfor %}'
            try:
                try:
                    if native:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {'values': Items()}
                        ).render('case', source)
                    else:
                        environment.from_string(source).render(values=Items())
                except NameError as error:
                    context = error.__context__
                    return context is not None and context.__cause__ is failure, context.args if context else None, events
                raise AssertionError('expected missing global lookup to fail')
            finally:
                runtime_module.missing = original

        expected = observe(False)
        self.assertEqual(expected, (True, ('end value',), ['first', 'delete']))
        self.assertEqual(observe(True), expected)

    def test_adapted_lookahead_uses_live_async_iterator_next(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            absent = object()
            previous = async_utils.__dict__.get('next', absent)

            def step(iterator: Any) -> Any:
                events.append('next')
                return next(iterator)

            def change() -> str:
                async_utils.__dict__['next'] = step
                return ''

            source = '{% for x in values %}{{ change() }}{{ loop.last }}{% break %}{% endfor %}'
            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': iter((1, 2)), 'change': change}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values=iter((1, 2)), change=change)
                return output, events
            finally:
                if previous is absent:
                    async_utils.__dict__.pop('next', None)
                else:
                    async_utils.__dict__['next'] = previous

        expected = observe(False)
        self.assertEqual(expected, ('False', ['next']))
        self.assertEqual(observe(True), expected)

    def test_adapted_lookahead_uses_live_stop_iteration_handler(self) -> None:
        class CustomStop(Exception):
            value = 'custom end'

        for native in (False, True):
            with self.subTest(native=native):
                failure = CustomStop('end')
                events: list[str] = []
                absent = object()
                previous_next = async_utils.__dict__.get('next', absent)
                previous_stop = async_utils.__dict__.get('StopIteration', absent)
                previous_missing = runtime_module.missing

                def step(iterator: Any) -> Any:
                    events.append('next')
                    del runtime_module.missing
                    raise failure

                def change() -> str:
                    async_utils.__dict__['next'] = step
                    async_utils.__dict__['StopIteration'] = CustomStop
                    return ''

                source = '{% for x in values %}{{ change() }}{{ loop.last }}{% endfor %}'
                environment = jinja2.Environment(enable_async=True)
                try:
                    with self.assertRaises(NameError) as caught:
                        if native:
                            docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': iter((1, 2)), 'change': change}
                            ).render('case', source)
                        else:
                            environment.from_string(source).render(values=iter((1, 2)), change=change)
                    context = caught.exception.__context__
                    assert context is not None
                    self.assertIs(context.__cause__, failure)
                    self.assertEqual(context.args, ('custom end',))
                    self.assertEqual(events, ['next'])
                finally:
                    runtime_module.missing = previous_missing
                    for name, previous in (('next', previous_next), ('StopIteration', previous_stop)):
                        if previous is absent:
                            async_utils.__dict__.pop(name, None)
                        else:
                            async_utils.__dict__[name] = previous

    def test_adapted_lookahead_invalid_stop_handler_chains_original(self) -> None:
        for native in (False, True):
            with self.subTest(native=native):
                failure = ValueError('next failed')
                absent = object()
                previous_next = async_utils.__dict__.get('next', absent)
                previous_stop = async_utils.__dict__.get('StopIteration', absent)

                def step(iterator: Any) -> Any:
                    raise failure

                def change() -> str:
                    async_utils.__dict__['next'] = step
                    async_utils.__dict__['StopIteration'] = 42
                    return ''

                source = '{% for x in values %}{{ change() }}{{ loop.last }}{% endfor %}'
                environment = jinja2.Environment(enable_async=True)
                try:
                    with self.assertRaises(TypeError) as caught:
                        if native:
                            docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': iter((1, 2)), 'change': change}
                            ).render('case', source)
                        else:
                            environment.from_string(source).render(values=iter((1, 2)), change=change)
                    self.assertIs(caught.exception.__context__, failure)
                finally:
                    for name, previous in (('next', previous_next), ('StopIteration', previous_stop)):
                        if previous is absent:
                            async_utils.__dict__.pop(name, None)
                        else:
                            async_utils.__dict__[name] = previous

    def test_adapted_lookahead_retains_original_iterator_class(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            original = async_utils._IteratorToAsyncIterator

            class Replacement:
                pass

            def change() -> str:
                events.append('change')
                async_utils.__dict__['_IteratorToAsyncIterator'] = Replacement
                return ''

            source = '{% for x in values %}{{ change() }}{{ loop.last }}{% break %}{% endfor %}'
            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': iter((1, 2)), 'change': change}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values=iter((1, 2)), change=change)
                return output, events
            finally:
                async_utils.__dict__['_IteratorToAsyncIterator'] = original

        expected = observe(False)
        self.assertEqual(expected, ('False', ['change']))
        self.assertEqual(observe(True), expected)

    def test_adapted_iterator_class_is_captured_before_iter_callback(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            original = async_utils._IteratorToAsyncIterator

            class Replacement:
                pass

            class Items:
                def __iter__(self) -> Any:
                    events.append('iter')
                    async_utils.__dict__['_IteratorToAsyncIterator'] = Replacement
                    return iter((1, 2))

            source = '{% for x in values %}{{ loop.last }}{% break %}{% endfor %}'
            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': Items()}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values=Items())
                return output, events
            finally:
                async_utils.__dict__['_IteratorToAsyncIterator'] = original

        expected = observe(False)
        self.assertEqual(expected, ('False', ['iter']))
        self.assertEqual(observe(True), expected)

    def test_adapted_lookahead_calls_replaced_anext(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            cls = async_utils._IteratorToAsyncIterator
            original = cls.__anext__

            async def replacement(self: Any) -> int:
                events.append('replacement')
                return 42

            def change() -> str:
                setattr(cls, '__anext__', replacement)  # noqa: B010 - test a live method change
                return ''

            source = '{% for x in values %}{{ change() }}{{ loop.last }}{% break %}{% endfor %}'
            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': iter((1, 2)), 'change': change}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values=iter((1, 2)), change=change)
                return output, events
            finally:
                setattr(cls, '__anext__', original)  # noqa: B010 - restore the original method

        expected = observe(False)
        self.assertEqual(expected, ('False', ['replacement']))
        self.assertEqual(observe(True), expected)

    def test_adapted_iterator_does_not_retain_anext_function(self) -> None:
        def observe(native: bool) -> tuple[str, list[int]]:
            method = async_utils._IteratorToAsyncIterator.__anext__
            counts: list[int] = []

            def inspect() -> str:
                counts.append(sys.getrefcount(method))
                return ''

            source = '{% for x in values %}{{ inspect() }}{{ loop.last }}{% break %}{% endfor %}'
            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'values': iter((1, 2)), 'inspect': inspect}
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values=iter((1, 2)), inspect=inspect)
            return output, counts

        self.assertEqual(observe(True), observe(False))

    def test_adapted_iterator_does_not_add_observable_weakrefs(self) -> None:
        def observe(native: bool) -> tuple[str, list[int]]:
            method = async_utils._IteratorToAsyncIterator.__anext__
            counts: list[int] = []

            def inspect() -> str:
                counts.append(len(weakref.getweakrefs(method)))
                return ''

            source = '{% for x in values %}{{ inspect() }}{{ loop.last }}{% break %}{% endfor %}'
            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'values': iter((1, 2)), 'inspect': inspect}
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values=iter((1, 2)), inspect=inspect)
            return output, counts

        self.assertEqual(observe(True), observe(False))

    def test_adapted_anext_replaced_before_loop_construction(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            cls = async_utils._IteratorToAsyncIterator
            original = cls.__anext__

            async def replacement(self: Any) -> int:
                events.append('replacement')
                return 42

            source = '{% for x in values %}{{ x }}{% break %}{% endfor %}'
            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            try:
                setattr(cls, '__anext__', replacement)  # noqa: B010 - test a method changed before construction
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': iter((1, 2))}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values=iter((1, 2)))
                return output, events
            finally:
                setattr(cls, '__anext__', original)  # noqa: B010 - restore the original method

        expected = observe(False)
        self.assertEqual(expected, ('42', ['replacement']))
        self.assertEqual(observe(True), expected)

    def test_adapted_anext_replaced_before_metadata_loop(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            cls = async_utils._IteratorToAsyncIterator
            original = cls.__anext__

            async def replacement(self: Any) -> int:
                events.append('replacement')
                return 42

            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            source = '{% for x in values %}{{ x }}:{{ loop.index }}{% break %}{% endfor %}'
            try:
                setattr(cls, '__anext__', replacement)  # noqa: B010 - test an existing override
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': iter((1, 2))}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values=iter((1, 2)))
                return output, events
            finally:
                setattr(cls, '__anext__', original)  # noqa: B010 - restore the original method

        self.assertEqual(observe(False), ('42:1', ['replacement']))
        self.assertEqual(observe(True), ('42:1', ['replacement']))

    def test_adapted_anext_replaced_during_plain_loop(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            cls = async_utils._IteratorToAsyncIterator
            original = cls.__anext__

            async def replacement(self: Any) -> int:
                events.append('replacement')
                return 42

            def change() -> str:
                setattr(cls, '__anext__', replacement)  # noqa: B010 - test a live override
                return ''

            source = '{% for x in values %}{{ x }}{{ change() }}{% if x == 42 %}{% break %}{% endif %}{% endfor %}'
            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': iter((1, 2)), 'change': change}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values=iter((1, 2)), change=change)
                return output, events
            finally:
                setattr(cls, '__anext__', original)  # noqa: B010 - restore the original method

        self.assertEqual(observe(False), ('142', ['replacement']))
        self.assertEqual(observe(True), ('142', ['replacement']))

    def test_adapted_anext_replaced_before_native_import(self) -> None:
        source = textwrap.dedent('''
            import jinja2
            import jinja2.async_utils as async_utils
            from typing import Any

            async def replacement(self: Any) -> int:
                return 42

            async_utils._IteratorToAsyncIterator.__anext__ = replacement
            import docxtpl_native
            from docxtpl_native.bridge import BridgeConfig

            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            text = '{% for x in values %}{{ x }}{% break %}{% endfor %}'
            session = docxtpl_native._extension().PythonRenderSession(
                BridgeConfig(environment, False), {'values': iter((1, 2))}
            )
            print(session.render('case', text))
        ''')
        result = subprocess.run([sys.executable, '-c', source], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), '42')

    def test_adapted_constructor_override_runs_before_loop(self) -> None:
        def observe(native: bool, metadata: bool) -> tuple[str, list[str]]:
            cls = async_utils._IteratorToAsyncIterator
            original = cls.__init__
            events: list[str] = []

            def replacement(self: Any, iterator: Any) -> None:
                events.append('init')
                self._iterator = iter((9,))

            suffix = ':{{ loop.index }}' if metadata else ''
            source = '{% for x in values %}{{ x }}' + suffix + '{% break %}{% endfor %}'
            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            try:
                setattr(cls, '__init__', replacement)  # noqa: B010 - test a user constructor
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': iter((1, 2))}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values=iter((1, 2)))
                return output, events
            finally:
                setattr(cls, '__init__', original)  # noqa: B010 - restore the original constructor

        for metadata in (False, True):
            with self.subTest(metadata=metadata):
                expected = ('9:1' if metadata else '9', ['init'])
                self.assertEqual(observe(False, metadata), expected)
                self.assertEqual(observe(True, metadata), expected)

    def test_adapted_aiter_override_runs_in_plain_loop(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            cls = async_utils._IteratorToAsyncIterator
            original = cls.__aiter__
            events: list[str] = []

            def replacement(self: Any) -> Any:
                events.append('aiter')
                return self

            source = '{% for x in values %}{{ x }}{% break %}{% endfor %}'
            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            try:
                setattr(cls, '__aiter__', replacement)  # noqa: B010 - test async iteration callback
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': iter((1, 2))}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values=iter((1, 2)))
                return output, events
            finally:
                setattr(cls, '__aiter__', original)  # noqa: B010 - restore async iteration callback

        self.assertEqual(observe(False), ('1', ['aiter']))
        self.assertEqual(observe(True), ('1', ['aiter']))

    def test_adapted_constructor_gets_original_iterator_after_iter(self) -> None:
        def observe(native: bool) -> tuple[str, list[tuple[str, bool]]]:
            cls = async_utils._IteratorToAsyncIterator
            original = cls.__init__
            iterator = iter((1, 2))
            events: list[tuple[str, bool]] = []

            class Items:
                def __iter__(self) -> Any:
                    events.append(('iter', True))
                    return iterator

            def replacement(self: Any, incoming: Any) -> None:
                events.append(('init', incoming is iterator))
                self._iterator = incoming

            source = '{% for x in values %}{{ x }}:{{ loop.index }}{% break %}{% endfor %}'
            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            try:
                setattr(cls, '__init__', replacement)  # noqa: B010 - observe the constructor boundary
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': Items()}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values=Items())
                return output, events
            finally:
                setattr(cls, '__init__', original)  # noqa: B010 - restore the original constructor

        expected = ('1:1', [('iter', True), ('init', True)])
        self.assertEqual(observe(False), expected)
        self.assertEqual(observe(True), expected)

    def test_adapted_aiter_can_return_another_iterator(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            cls = async_utils._IteratorToAsyncIterator
            original = cls.__aiter__
            events: list[str] = []

            async def values() -> Any:
                events.append('next')
                yield 9

            def replacement(self: Any) -> Any:
                events.append('aiter')
                return values()

            source = '{% for x in values %}{{ x }}{% break %}{% endfor %}'
            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            try:
                setattr(cls, '__aiter__', replacement)  # noqa: B010 - return a different async iterator
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': iter((1, 2))}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values=iter((1, 2)))
                return output, events
            finally:
                setattr(cls, '__aiter__', original)  # noqa: B010 - restore async iteration callback

        self.assertEqual(observe(False), ('9', ['aiter', 'next']))
        self.assertEqual(observe(True), ('9', ['aiter', 'next']))

    def test_replaced_adapted_anext_receives_real_iterator(self) -> None:
        cls = async_utils._IteratorToAsyncIterator
        original = cls.__anext__
        events: list[tuple[bool, bool]] = []
        iterator = iter((1, 2))

        async def replacement(self: Any) -> int:
            events.append((type(self) is cls, self._iterator is iterator))
            return 42

        def change() -> str:
            setattr(cls, '__anext__', replacement)  # noqa: B010 - test a live method change
            return ''

        environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
        source = '{% for x in values %}{{ change() }}{{ loop.last }}{% break %}{% endfor %}'
        try:
            session = docxtpl_native._extension().PythonRenderSession(
                BridgeConfig(environment, False), {'values': iterator, 'change': change}
            )
            self.assertEqual(session.render('case', source), 'False')
            self.assertEqual(events, [(True, True)])
        finally:
            setattr(cls, '__anext__', original)  # noqa: B010 - restore the original method

    def test_replaced_adapted_anext_keeps_adapter_identity(self) -> None:
        def observe(native: bool) -> tuple[str, list[bool]]:
            cls = async_utils._IteratorToAsyncIterator
            original = cls.__anext__
            retained: list[Any] = []

            async def replacement(self: Any) -> int:
                retained.append(self)
                return 42

            def change() -> str:
                setattr(cls, '__anext__', replacement)  # noqa: B010 - test a live method change
                return ''

            source = '{% for x in values %}{{ change() }}{{ loop.last }};{% if loop.index == 2 %}{% break %}{% endif %}{% endfor %}'
            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': iter((1, 2)), 'change': change}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values=iter((1, 2)), change=change)
                return output, [type(value) is cls and value is retained[0] for value in retained]
            finally:
                setattr(cls, '__anext__', original)  # noqa: B010 - restore the original method

        expected = observe(False)
        self.assertEqual(expected, ('False;False;', [True, True]))
        self.assertEqual(observe(True), expected)

    def test_replaced_adapted_anext_awaits_in_same_task(self) -> None:
        def observe(native: bool) -> tuple[str, list[bool]]:
            cls = async_utils._IteratorToAsyncIterator
            original = cls.__anext__
            events: list[bool] = []

            async def replacement(self: Any) -> int:
                task = asyncio.current_task()
                await asyncio.sleep(0)
                events.append(task is asyncio.current_task())
                return 42

            def change() -> str:
                setattr(cls, '__anext__', replacement)  # noqa: B010 - test an awaited callback
                return ''

            source = '{% for x in values %}{{ change() }}{{ loop.last }}{% break %}{% endfor %}'
            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': iter((1, 2)), 'change': change}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values=iter((1, 2)), change=change)
                return output, events
            finally:
                setattr(cls, '__anext__', original)  # noqa: B010 - restore the original method

        expected = observe(False)
        self.assertEqual(expected, ('False', [True]))
        self.assertEqual(observe(True), expected)

    def test_replaced_adapted_anext_preserves_exception_identity_and_cause(self) -> None:
        for native in (False, True):
            with self.subTest(native=native):
                cls = async_utils._IteratorToAsyncIterator
                original = cls.__anext__
                failure = ValueError('replacement failed')
                cause = LookupError('callback cause')
                events: list[str] = []

                async def replacement(self: Any) -> int:
                    events.append('before')
                    await asyncio.sleep(0)
                    events.append('raise')
                    raise failure from cause

                def change() -> str:
                    setattr(cls, '__anext__', replacement)  # noqa: B010 - test an awaited callback error
                    return ''

                source = '{% for x in values %}{{ change() }}{{ loop.last }}{% endfor %}'
                environment = jinja2.Environment(enable_async=True)
                try:
                    with self.assertRaises(ValueError) as caught:
                        if native:
                            docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': iter((1, 2)), 'change': change}
                            ).render('case', source)
                        else:
                            environment.from_string(source).render(values=iter((1, 2)), change=change)
                    self.assertIs(caught.exception, failure)
                    self.assertIs(caught.exception.__cause__, cause)
                    self.assertEqual(events, ['before', 'raise'])
                finally:
                    setattr(cls, '__anext__', original)  # noqa: B010 - restore the original method

    def test_replaced_adapted_anext_returning_missing_is_absent(self) -> None:
        for item_access in (False, True):
            with self.subTest(item_access=item_access):
                def observe(native: bool) -> tuple[str, list[str]]:
                    cls = async_utils._IteratorToAsyncIterator
                    original = cls.__anext__
                    events: list[str] = []

                    async def replacement(self: Any) -> Any:
                        events.append('replacement')
                        await asyncio.sleep(0)
                        return runtime_module.missing

                    def change() -> str:
                        setattr(cls, '__anext__', replacement)  # noqa: B010 - test a live method change
                        return ''

                    access = "loop['nextitem']" if item_access else 'loop.nextitem'
                    source = '{% for x in values %}{{ change() }}{{ ' + access + ' is undefined }}{% break %}{% endfor %}'
                    environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
                    try:
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': iter((1, 2)), 'change': change}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=iter((1, 2)), change=change)
                        return output, events
                    finally:
                        setattr(cls, '__anext__', original)  # noqa: B010 - restore the original method

                expected = observe(False)
                self.assertEqual(expected, ('True', ['replacement']))
                self.assertEqual(observe(True), expected)

    def test_replaced_adapted_anext_exhaustion_preserves_stop_context(self) -> None:
        def observe(native: bool) -> tuple[str, bool, list[str]]:
            cls = async_utils._IteratorToAsyncIterator
            original_method = cls.__anext__
            original_missing = runtime_module.missing
            failure = StopAsyncIteration('callback exhausted')
            events: list[str] = []

            async def replacement(self: Any) -> int:
                events.append('replacement')
                await asyncio.sleep(0)
                del runtime_module.missing
                raise failure

            def change() -> str:
                setattr(cls, '__anext__', replacement)  # noqa: B010 - test a live method change
                return ''

            source = '{% for x in values %}{{ change() }}{{ loop.last }}{% break %}{% endfor %}'
            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            try:
                try:
                    if native:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {'values': iter((1, 2)), 'change': change}
                        ).render('case', source)
                    else:
                        environment.from_string(source).render(values=iter((1, 2)), change=change)
                except NameError as error:
                    return str(error), error.__context__ is failure, events
                raise AssertionError('expected missing global lookup to fail')
            finally:
                runtime_module.missing = original_missing
                setattr(cls, '__anext__', original_method)  # noqa: B010 - restore the original method

        expected = observe(False)
        self.assertEqual(expected, ("name 'missing' is not defined", True, ['replacement']))
        self.assertEqual(observe(True), expected)

    def test_replaced_adapted_anext_can_replace_underlying_iterator(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            cls = async_utils._IteratorToAsyncIterator
            original = cls.__anext__
            events: list[str] = []

            async def replacement(self: Any) -> int:
                events.append('replacement')
                self._iterator = iter((7, 8))
                return 42

            def change(value: Any) -> str:
                if value == 1:
                    setattr(cls, '__anext__', replacement)  # noqa: B010 - test a live method change
                elif value == 42:
                    setattr(cls, '__anext__', original)  # noqa: B010 - test restoration during render
                return ''

            source = '{% for x in values %}{{ x }}{{ change(x) }}:{{ loop.last }};{% endfor %}'
            environment = jinja2.Environment(enable_async=True)
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': iter((1, 2)), 'change': change}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values=iter((1, 2)), change=change)
                return output, events
            finally:
                setattr(cls, '__anext__', original)  # noqa: B010 - restore the original method

        expected = observe(False)
        self.assertEqual(expected, ('1:False;42:False;7:False;8:True;', ['replacement']))
        self.assertEqual(observe(True), expected)

    def test_plain_loop_default_adapted_anext_body_remains_native(self) -> None:
        for native in (False, True):
            with self.subTest(native=native):
                target = async_utils._IteratorToAsyncIterator.__anext__.__code__
                previous = sys.getprofile()
                calls: list[str] = []

                def profile(frame: Any, event: str, argument: Any) -> None:
                    if event == 'call' and frame.f_code is target:
                        calls.append(frame.f_code.co_name)

                source = '{% for x in values %}{{ x }};{% endfor %}'
                environment = jinja2.Environment(enable_async=True)
                try:
                    sys.setprofile(profile)
                    if native:
                        output = docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {'values': iter((1, 2))}
                        ).render('case', source)
                    else:
                        output = environment.from_string(source).render(values=iter((1, 2)))
                finally:
                    sys.setprofile(previous)
                self.assertEqual(output, '1;2;')
                if native:
                    self.assertEqual(calls, [])
                else:
                    self.assertTrue(calls)

    def test_restored_adapted_anext_body_remains_native(self) -> None:
        for native in (False, True):
            with self.subTest(native=native):
                cls = async_utils._IteratorToAsyncIterator
                original = cls.__anext__
                target = original.__code__
                previous = sys.getprofile()
                calls: list[str] = []

                async def replacement(self: Any) -> int:
                    self._iterator = iter((7,))
                    return 42

                def change(value: Any) -> str:
                    if value == 1:
                        setattr(cls, '__anext__', replacement)  # noqa: B010 - test a live method change
                    elif value == 42:
                        setattr(cls, '__anext__', original)  # noqa: B010 - test restoration during render
                    return ''

                def profile(frame: Any, event: str, argument: Any) -> None:
                    if event == 'call' and frame.f_code is target:
                        calls.append(frame.f_code.co_name)

                source = '{% for x in values %}{{ x }}{{ change(x) }}:{{ loop.last }};{% endfor %}'
                environment = jinja2.Environment(enable_async=True)
                try:
                    sys.setprofile(profile)
                    if native:
                        output = docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {'values': iter((1, 2)), 'change': change}
                        ).render('case', source)
                    else:
                        output = environment.from_string(source).render(values=iter((1, 2)), change=change)
                finally:
                    sys.setprofile(previous)
                    setattr(cls, '__anext__', original)  # noqa: B010 - restore the original method
                self.assertEqual(output, '1:False;42:False;7:True;')
                if native:
                    self.assertEqual(calls, [])
                else:
                    self.assertTrue(calls)

    def test_adapted_anext_after_length_materialization_remains_native(self) -> None:
        for native in (False, True):
            with self.subTest(native=native):
                target = async_utils._IteratorToAsyncIterator.__anext__.__code__
                previous = sys.getprofile()
                calls: list[str] = []

                def profile(frame: Any, event: str, argument: Any) -> None:
                    if event == 'call' and frame.f_code is target:
                        calls.append(frame.f_code.co_name)

                environment = jinja2.Environment(enable_async=True)
                source = '{% for x in values %}{{ loop.length }}:{{ x }};{% endfor %}'
                try:
                    sys.setprofile(profile)
                    if native:
                        output = docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {'values': iter((1, 2, 3))}
                        ).render('case', source)
                    else:
                        output = environment.from_string(source).render(values=iter((1, 2, 3)))
                finally:
                    sys.setprofile(previous)
                self.assertEqual(output, '3:1;3:2;3:3;')
                if native:
                    self.assertEqual(calls, [])
                else:
                    self.assertTrue(calls)

    def test_document_replaced_adapted_anext_matches_reference(self) -> None:
        source = '{% for x in values %}{{ change() }}{{ loop.last }}{% break %}{% endfor %}'
        document = template(source)

        def run(native: bool) -> tuple[bytes, list[bool]]:
            cls = async_utils._IteratorToAsyncIterator
            original = cls.__anext__
            iterator = iter((1, 2))
            events: list[bool] = []

            async def replacement(self: Any) -> int:
                events.append(type(self) is cls and self._iterator is iterator)
                await asyncio.sleep(0)
                return 42

            def change() -> str:
                setattr(cls, '__anext__', replacement)  # noqa: B010 - test a live method change
                return ''

            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            try:
                output = generate(docxtpl_native if native else docxtpl, source,
                                  {'values': iterator, 'change': change}, environment, source=document)
                return output, events
            finally:
                setattr(cls, '__anext__', original)  # noqa: B010 - restore the original method

        reference, expected = run(False)
        candidate, actual = run(True)
        self.assertEqual(expected, [True])
        self.assertEqual(actual, expected)
        comparison = compare_docx(reference, candidate)
        self.assertTrue(comparison['equivalent'], comparison)

    def test_document_lookahead_rechecks_live_missing(self) -> None:
        source = '{% for x in values %}{{ loop.last }}{% break %}{% endfor %}'
        document = template(source)

        def run(native: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []
            original = runtime_module.missing

            def values() -> Any:
                events.append('first')
                yield 1
                events.append('change')
                runtime_module.missing = object()

            environment = jinja2.Environment(extensions=['jinja2.ext.loopcontrols'])
            try:
                output = generate(docxtpl_native if native else docxtpl, source, {'values': values()},
                                  environment, source=document)
                return output, events
            finally:
                runtime_module.missing = original

        reference, expected = run(False)
        candidate, actual = run(True)
        self.assertEqual(expected, ['first', 'change'])
        self.assertEqual(actual, expected)
        comparison = compare_docx(reference, candidate)
        self.assertTrue(comparison['equivalent'], comparison)

    def test_sandbox_lookahead_yielding_missing(self) -> None:
        for asynchronous in (False, True):
            for item_access in (False, True):
                with self.subTest(asynchronous=asynchronous, item_access=item_access):
                    def run(native: bool) -> tuple[str, list[str]]:
                        events: list[str] = []

                        def values() -> Any:
                            events.append('first')
                            yield 1
                            events.append('missing')
                            yield runtime_module.missing

                        access = "loop['last']" if item_access else 'loop.last'
                        source = '{% for x in values %}{{ ' + access + ' }}{% break %}{% endfor %}'
                        environment = SandboxedEnvironment(enable_async=asynchronous, extensions=['jinja2.ext.loopcontrols'])
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': values()}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=values())
                        return output, events

                    expected = run(False)
                    self.assertEqual(expected, ('True', ['first', 'missing']))
                    self.assertEqual(run(True), expected)

    def test_yielded_missing_is_undefined_for_nextitem(self) -> None:
        for asynchronous in (False, True):
            for item_access in (False, True):
                with self.subTest(asynchronous=asynchronous, item_access=item_access):
                    def run(native: bool) -> tuple[str, list[str]]:
                        events: list[str] = []

                        def values() -> Any:
                            events.append('first')
                            yield 1
                            events.append('missing')
                            yield runtime_module.missing

                        access = "loop['nextitem']" if item_access else 'loop.nextitem'
                        source = '{% for x in values %}{{ ' + access + ' is undefined }}{% break %}{% endfor %}'
                        environment = jinja2.Environment(enable_async=asynchronous, extensions=['jinja2.ext.loopcontrols'])
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': values()}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=values())
                        return output, events

                    expected = run(False)
                    self.assertEqual(expected, ('True', ['first', 'missing']))
                    self.assertEqual(run(True), expected)

    def test_async_lookahead_does_not_probe_yielded_item_type(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Meta(type):
                def __getattribute__(cls, name: str) -> Any:
                    if name == '__iter__':
                        events.append('type-iter')
                    return super().__getattribute__(name)

            class Item(metaclass=Meta):
                pass

            def values() -> Any:
                yield 1
                yield Item()

            source = '{% for x in values %}{{ loop.last }}{% break %}{% endfor %}'
            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'values': values()}
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values=values())
            return output, events

        self.assertEqual(run(True), run(False))

    def test_standard_lookahead_bodies_remain_native(self) -> None:
        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    classes = (runtime_module.LoopContext, runtime_module.AsyncLoopContext)
                    targets = {cls.__dict__[name].__code__ for cls in classes for name in ('__next__', '__anext__', '_peek_next')
                               if name in cls.__dict__}
                    targets.add(async_utils._IteratorToAsyncIterator.__anext__.__code__)
                    calls: list[str] = []
                    previous = sys.getprofile()

                    def profile(frame: Any, event: str, argument: Any) -> None:
                        if event == 'call' and frame.f_code in targets:
                            calls.append(frame.f_code.co_name)

                    environment = jinja2.Environment(enable_async=asynchronous)
                    source = '{% for x in values %}{{ loop.last }};{% endfor %}'
                    try:
                        sys.setprofile(profile)
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': [1, 2]}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=[1, 2])
                    finally:
                        sys.setprofile(previous)
                    self.assertEqual(output, 'False;True;')
                    if native:
                        self.assertEqual(calls, [])
                    else:
                        self.assertTrue(calls)

    def test_missing_change_before_lookahead_preserves_cached_state(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool) -> tuple[str, list[str]]:
                    events: list[str] = []
                    original = runtime_module.missing

                    def values() -> Any:
                        events.append('first')
                        yield 1
                        events.append('second')
                        yield 2

                    def change() -> str:
                        events.append('change')
                        runtime_module.missing = object()
                        return ''

                    source = '{% for x in values %}{{ change() }}{{ loop.last }}{% break %}{% endfor %}'
                    environment = jinja2.Environment(enable_async=asynchronous, extensions=['jinja2.ext.loopcontrols'])
                    try:
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': values(), 'change': change}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=values(), change=change)
                        return output, events
                    finally:
                        runtime_module.missing = original

                expected = observe(False)
                self.assertEqual(expected, ('False', ['first', 'change']))
                self.assertEqual(observe(True), expected)

    def test_missing_change_after_lookahead_rechecks_cached_item(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool) -> tuple[str, list[str]]:
                    events: list[str] = []
                    original = runtime_module.missing
                    marker = object()

                    def values() -> Any:
                        for label, value in (('first', 1), ('marker', marker), ('third', 3)):
                            events.append(label)
                            yield value

                    def record(value: Any) -> str:
                        return 'marker' if value is marker else str(value)

                    def change(value: Any) -> str:
                        if value == 1:
                            events.append('change')
                            runtime_module.missing = marker
                        return ''

                    source = '{% for x in values %}{{ record(x) }}:{{ loop.last }};{{ change(x) }}{% endfor %}'
                    environment = jinja2.Environment(enable_async=asynchronous)
                    try:
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': values(), 'record': record, 'change': change}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=values(), record=record, change=change)
                        return output, events
                    finally:
                        runtime_module.missing = original

                expected = observe(False)
                self.assertEqual(expected, ('1:False;3:True;', ['first', 'marker', 'change', 'third']))
                self.assertEqual(observe(True), expected)

    def test_prelookahead_missing_change_retains_previous_sentinel_as_next_item(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool) -> tuple[str, list[str]]:
                    events: list[str] = []
                    original = runtime_module.missing

                    def values() -> Any:
                        events.append('first')
                        yield 1
                        events.append('second')
                        yield 2

                    def describe(value: Any) -> str:
                        return 'old' if value is original else str(value)

                    def change(value: Any) -> str:
                        if value == 1:
                            events.append('change')
                            runtime_module.missing = object()
                        return ''

                    source = '{% for x in values %}{{ describe(x) }}{{ change(x) }}:{{ loop.last }};{% endfor %}'
                    environment = jinja2.Environment(enable_async=asynchronous)
                    try:
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': values(), 'describe': describe, 'change': change}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=values(), describe=describe, change=change)
                        return output, events
                    finally:
                        runtime_module.missing = original

                expected = observe(False)
                self.assertEqual(expected, ('1:False;old:False;2:True;', ['first', 'change', 'second']))
                self.assertEqual(observe(True), expected)

    def test_missing_change_between_two_lookaheads_rechecks_cached_item(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool) -> tuple[str, list[str]]:
                    events: list[str] = []
                    original = runtime_module.missing
                    marker = object()

                    def values() -> Any:
                        for label, value in (('first', 1), ('marker', marker), ('third', 3)):
                            events.append(label)
                            yield value

                    def change() -> str:
                        events.append('change')
                        runtime_module.missing = marker
                        return ''

                    source = '{% for x in values %}{{ loop.last }}:{{ change() }}{{ loop.last }}{% break %}{% endfor %}'
                    environment = jinja2.Environment(enable_async=asynchronous, extensions=['jinja2.ext.loopcontrols'])
                    try:
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': values(), 'change': change}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=values(), change=change)
                        return output, events
                    finally:
                        runtime_module.missing = original

                expected = observe(False)
                self.assertEqual(expected, ('False:False', ['first', 'marker', 'change', 'third']))
                self.assertEqual(observe(True), expected)

    def observe(self, native: bool, asynchronous: bool, sandboxed: bool,
                expression: str, filtered: bool) -> tuple[str, list[tuple[str, int]]]:
        events: list[tuple[str, int]] = []

        def values() -> Any:
            for value in (1, 2, 3):
                events.append(('next', value))
                yield value

        def selected(value: int) -> bool:
            events.append(('filter', value))
            return value % 2 == 1

        def emit(value: int) -> int:
            events.append(('body', value))
            return value

        environment = (SandboxedEnvironment if sandboxed else jinja2.Environment)(enable_async=asynchronous)
        environment.tests['selected'] = selected
        suffix = ' if x is selected' if filtered else ''
        source = '{% for x in values' + suffix + ' %}{{ emit(x) }}:{{ ' + expression + ' }};{% endfor %}'
        if native:
            result = docxtpl_native._extension().PythonRenderSession(
                BridgeConfig(environment, False), {'values': values(), 'emit': emit}
            ).render('case', source)
        else:
            result = environment.from_string(source).render(values=values(), emit=emit)
        return result, events

    def test_loop_item_metadata_and_methods(self) -> None:
        expressions = ("loop['index']", "loop['last']", "loop['length']",
                       "loop['cycle']('a', 'b')", "loop['changed'](x)",
                       'loop.index', "loop.cycle('a', 'b')", 'loop.changed(x)')
        for asynchronous in (False, True):
            for sandboxed in (False, True):
                for filtered in (False, True):
                    for expression in expressions:
                        with self.subTest(asynchronous=asynchronous, sandboxed=sandboxed,
                                          filtered=filtered, expression=expression):
                            self.assertEqual(
                                self.observe(True, asynchronous, sandboxed, expression, filtered),
                                self.observe(False, asynchronous, sandboxed, expression, filtered),
                            )

    def test_complete_document(self) -> None:
        source = "{% for x in values %}{{ loop['index'] }}:{{ loop['cycle']('a', 'b') }};{% endfor %}"
        original = template(source)
        context = {'values': [1, 2, 3]}
        reference = generate(docxtpl, source, context, source=original)
        candidate = generate(docxtpl_native, source, context, source=original)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_changed_item_lookup_global_matches_reference(self) -> None:
        for asynchronous in (False, True):
            for sandboxed in (False, True):
                with self.subTest(asynchronous=asynchronous, sandboxed=sandboxed):
                    module = sandbox_module if sandboxed else environment_module
                    missing = object()
                    previous = module.__dict__.get('isinstance', missing)

                    def deny(value: object, kind: object) -> bool:
                        return False

                    def change(target: Any = module, callback: Any = deny) -> str:
                        target.__dict__['isinstance'] = callback
                        return ''

                    source = "{{ change() }}{% for x in [1] %}{{ loop['index'] }}{% endfor %}"
                    try:
                        environment = (SandboxedEnvironment if sandboxed else jinja2.Environment)(enable_async=asynchronous)
                        reference = environment.from_string(source).render(change=change)
                        self.assertEqual(reference, '')
                        module.__dict__.pop('isinstance', None)
                        if previous is not missing:
                            module.__dict__['isinstance'] = previous
                        environment = (SandboxedEnvironment if sandboxed else jinja2.Environment)(enable_async=asynchronous)
                        session = docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {'change': change}
                        )
                        self.assertEqual(session.render('case', source), reference)
                    finally:
                        if previous is missing:
                            module.__dict__.pop('isinstance', None)
                        else:
                            module.__dict__['isinstance'] = previous

    def test_changed_attribute_lookup_global_matches_reference(self) -> None:
        for asynchronous in (False, True):
            for sandboxed in (False, True):
                with self.subTest(asynchronous=asynchronous, sandboxed=sandboxed):
                    module = sandbox_module if sandboxed else environment_module
                    missing = object()
                    previous = module.__dict__.get('getattr', missing)

                    def altered(value: Any, name: str, *default: Any) -> Any:
                        if name == 'index':
                            return 99
                        return getattr(value, name, *default)

                    def change(target: Any = module, callback: Any = altered) -> str:
                        target.__dict__['getattr'] = callback
                        return ''

                    source = '{{ change() }}{% for x in [1] %}{{ loop.index }}{% endfor %}'
                    try:
                        environment = (SandboxedEnvironment if sandboxed else jinja2.Environment)(enable_async=asynchronous)
                        reference = environment.from_string(source).render(change=change)
                        self.assertEqual(reference, '99')
                        module.__dict__.pop('getattr', None)
                        if previous is not missing:
                            module.__dict__['getattr'] = previous
                        environment = (SandboxedEnvironment if sandboxed else jinja2.Environment)(enable_async=asynchronous)
                        session = docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {'change': change}
                        )
                        self.assertEqual(session.render('case', source), reference)
                    finally:
                        if previous is missing:
                            module.__dict__.pop('getattr', None)
                        else:
                            module.__dict__['getattr'] = previous

    def test_lookup_keeps_getattr_captured_before_lookahead(self) -> None:
        def observe(native: bool, asynchronous: bool, sandboxed: bool, item_access: bool) -> tuple[str, list[int]]:
            module = sandbox_module if sandboxed else environment_module
            missing = object()
            previous = module.__dict__.get('getattr', missing)
            events: list[int] = []

            def values() -> Any:
                for value in (1, 2):
                    events.append(value)
                    if value == 2:
                        module.__dict__['getattr'] = lambda obj, name: 'changed'
                    yield value

            access = "loop['last']" if item_access else 'loop.last'
            source = '{% for x in items %}{{ ' + access + ' }}{% break %}{% endfor %}'
            environment = (SandboxedEnvironment if sandboxed else jinja2.Environment)(
                enable_async=asynchronous, extensions=['jinja2.ext.loopcontrols']
            )
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'items': values()}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(items=values())
                return output, events
            finally:
                if previous is missing:
                    module.__dict__.pop('getattr', None)
                else:
                    module.__dict__['getattr'] = previous

        for asynchronous in (False, True):
            for sandboxed in (False, True):
                for item_access in (False, True):
                    with self.subTest(asynchronous=asynchronous, sandboxed=sandboxed, item_access=item_access):
                        self.assertEqual(observe(True, asynchronous, sandboxed, item_access),
                                         observe(False, asynchronous, sandboxed, item_access))

    def test_item_loop_methods_do_not_execute_reference_algorithms(self) -> None:
        forbidden = {LoopContext.changed.__code__, LoopContext.cycle.__code__}
        previous = sys.getprofile()
        source = "{% for x in [1, 1] %}{{ loop['cycle']('a', 'b') }}{{ loop['changed'](x) }}{% endfor %}"

        def profile(frame: Any, event: str, argument: Any) -> None:
            if event == 'call' and frame.f_code in forbidden:
                raise AssertionError('reference loop algorithm executed')

        try:
            sys.setprofile(profile)
            for asynchronous in (False, True):
                environment = jinja2.Environment(enable_async=asynchronous)
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {}
                ).render('case', source)
                self.assertEqual(output, 'aTruebFalse')
        finally:
            sys.setprofile(previous)


if __name__ == '__main__':
    unittest.main()
