"""Laboratory checks for direct awaiting inside one asyncio task."""
import argparse
import asyncio
import builtins
import contextvars
import importlib.util
import inspect
import jinja2.async_utils
import threading
import types
import unittest


MODULE = None


async def reference(callbacks):
    result = []
    for callback in callbacks:
        value = callback()
        result.append(await value if inspect.isawaitable(value) else value)
    return result


class DirectAsyncTests(unittest.TestCase):
    def test_await_uses_type_slot_not_instance_attribute(self):
        class Awaitable:
            def __await__(self):
                if False:yield
                return 'done'
            def __getattribute__(self, name):
                if name == '__await__':raise AssertionError('instance lookup used')
                return object.__getattribute__(self, name)
        async def run(mode):
            callbacks = [lambda: Awaitable()]
            return await (reference(callbacks) if mode == 'reference' else getattr(MODULE, mode)(callbacks))
        expected = asyncio.run(run('reference'))
        self.assertEqual(expected, ['done'])
        for mode in ('run_direct', 'run_from_worker'):
            with self.subTest(mode=mode):
                self.assertEqual(asyncio.run(run(mode)), expected)

    def test_invalid_await_iterator_errors_and_causes(self):
        class Bad:
            def __init__(self, failure):self.failure = failure
            def __await__(self):
                if self.failure:raise ValueError('await setup failed')
                return 42
        class Values:
            def __init__(self, failure):self.failure = failure
            def __aiter__(self):return self
            def __anext__(self):return Bad(self.failure)
        async def run(mode, failure):
            try:
                if mode == 'reference':
                    return [x async for x in jinja2.async_utils.auto_aiter(Values(failure))]
                return await MODULE.collect_iteration(Values(failure))
            except Exception as error:
                cause = error.__cause__
                return type(error), str(error), type(cause), str(cause)
        for failure in (False, True):
            with self.subTest(failure=failure):
                self.assertEqual(asyncio.run(run('native', failure)), asyncio.run(run('reference', failure)))

    def test_await_method_must_not_return_generator_coroutine(self):
        @types.coroutine
        def coroutine_generator():
            yield
        class Invalid:
            def __await__(self):return coroutine_generator()
        async def run(mode):
            try:
                callbacks = [Invalid]
                return await (reference(callbacks) if mode == 'reference' else getattr(MODULE, mode)(callbacks))
            except Exception as error:
                return type(error), str(error)
        expected = asyncio.run(run('reference'))
        self.assertEqual(expected, (TypeError, '__await__() returned a coroutine'))
        for mode in ('run_direct', 'run_from_worker'):
            with self.subTest(mode=mode):
                self.assertEqual(asyncio.run(run(mode)), expected)

    def test_cancel_wins_over_pending_yield_error(self):
        async def run(mode):
            other = asyncio.new_event_loop()
            future = other.create_future()
            async def callback():
                task = asyncio.current_task()
                asyncio.get_running_loop().call_soon(task.cancel)
                try:
                    await future
                except asyncio.CancelledError:
                    await asyncio.sleep(0)
                    return 'recovered'
                except RuntimeError:
                    return 'wrong error'
            try:
                coroutine = reference([callback]) if mode == 'reference' else getattr(MODULE, mode)([callback])
                return await asyncio.wait_for(coroutine, 1)
            finally:
                future.cancel()
                other.close()
        expected = asyncio.run(run('reference'))
        self.assertEqual(expected, ['recovered'])
        for mode in ('run_direct', 'run_from_worker'):
            with self.subTest(mode=mode):
                self.assertEqual(asyncio.run(run(mode)), expected)

    def test_cancel_wins_over_pending_bad_yield(self):
        @types.coroutine
        def callback():
            asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
            try:
                yield 'bad'
            except asyncio.CancelledError:
                yield
                return 'recovered'
            except RuntimeError:
                return 'wrong error'
        async def run(mode):
            coroutine = reference([callback]) if mode == 'reference' else getattr(MODULE, mode)([callback])
            return await asyncio.wait_for(coroutine, 1)
        expected = asyncio.run(run('reference'))
        self.assertEqual(expected, ['recovered'])
        for mode in ('run_direct', 'run_from_worker'):
            with self.subTest(mode=mode):
                self.assertEqual(asyncio.run(run(mode)), expected)

    def test_await_iterator_uses_next_protocol_not_instance_attribute(self):
        class Iterator:
            def __iter__(self):return self
            def __next__(self):raise StopIteration('done')
            def __getattribute__(self, name):
                if name == '__next__':raise AssertionError('instance lookup used')
                return object.__getattribute__(self, name)
        class Awaitable:
            def __await__(self):return Iterator()
        async def run(mode):
            callbacks = [lambda: Awaitable()]
            return await (reference(callbacks) if mode == 'reference' else getattr(MODULE, mode)(callbacks))
        expected = asyncio.run(run('reference'))
        self.assertEqual(expected, ['done'])
        for mode in ('run_direct', 'run_from_worker'):
            with self.subTest(mode=mode):
                self.assertEqual(asyncio.run(run(mode)), expected)

    def test_async_iteration_does_not_use_builtin_anext(self):
        async def values():
            yield 1
            yield 2
        async def run(mode):
            if mode == 'reference':
                return [value async for value in jinja2.async_utils.auto_aiter(values())]
            return await MODULE.collect_iteration(values())
        saved = builtins.anext
        def broken(*args):
            raise AssertionError('builtin anext unexpectedly invoked')
        try:
            builtins.anext = broken
            expected = asyncio.run(run('reference'))
            self.assertEqual(expected, [1, 2])
            self.assertEqual(asyncio.run(run('native')), expected)
        finally:
            builtins.anext = saved

    def test_async_iteration_does_not_use_builtin_aiter(self):
        async def values():
            yield 1
        async def run(mode):
            if mode == 'reference':
                return [value async for value in jinja2.async_utils.auto_aiter(values())]
            return await MODULE.collect_iteration(values())
        saved = builtins.aiter
        def broken(*args):
            raise AssertionError('builtin aiter unexpectedly invoked')
        try:
            builtins.aiter = broken
            expected = asyncio.run(run('reference'))
            self.assertEqual(asyncio.run(run('native')), expected)
        finally:
            builtins.aiter = saved

    def test_async_iteration_invalid_iterators(self):
        async def run(mode, result):
            class Source:
                def __aiter__(self):return result
            try:
                if mode == 'reference':
                    return [value async for value in jinja2.async_utils.auto_aiter(Source())]
                return await MODULE.collect_iteration(Source())
            except Exception as error:
                return type(error), str(error)
        class NoNext:
            def __aiter__(self):return self
        for result in (42, NoNext()):
            with self.subTest(result=type(result)):
                self.assertEqual(asyncio.run(run('native', result)), asyncio.run(run('reference', result)))

    def test_async_iterator_with_generator_coroutine(self):
        class Values:
            def __init__(self):self.index = 0
            def __aiter__(self):return self
            @types.coroutine
            def __anext__(self):
                yield
                if self.index == 2:raise StopAsyncIteration
                self.index += 1
                return self.index
        async def run(mode):
            if mode == 'reference':
                return [value async for value in jinja2.async_utils.auto_aiter(Values())]
            return await MODULE.collect_iteration(Values())
        self.assertEqual(asyncio.run(run('native')), asyncio.run(run('reference')))

    def test_async_and_sync_iteration_trace(self):
        async def run(mode, asynchronous):
            trace = []
            task = asyncio.current_task()
            class AsyncValues:
                def __init__(self):
                    self.index = 0
                def __aiter__(self):
                    trace.append('aiter')
                    return self
                async def __anext__(self):
                    self.assert_task()
                    await asyncio.sleep(0)
                    if self.index == 2:
                        trace.append('end')
                        raise StopAsyncIteration
                    self.index += 1
                    trace.append('yield:' + str(self.index))
                    return self.index
                def assert_task(self):
                    if asyncio.current_task() is not task:
                        raise AssertionError('changed task')
            class SyncValues:
                def __iter__(self):
                    trace.append('iter')
                    for value in (1, 2):
                        trace.append('yield:' + str(value))
                        yield value
                    trace.append('end')
            source = AsyncValues() if asynchronous else SyncValues()
            if mode == 'reference':
                values = [value async for value in jinja2.async_utils.auto_aiter(source)]
            else:
                values = await MODULE.collect_iteration(source)
            return values, trace
        for asynchronous in (False, True):
            expected = asyncio.run(run('reference', asynchronous))
            self.assertEqual(asyncio.run(run('native', asynchronous)), expected)

    def test_async_iteration_exception_and_invalid_anext(self):
        async def run(mode, invalid):
            failure = ValueError('iteration failed')
            class Broken:
                def __aiter__(self):return self
                def __anext__(self):
                    if invalid:return 42
                    async def fail():
                        await asyncio.sleep(0)
                        raise failure
                    return fail()
            try:
                if mode == 'reference':
                    return [value async for value in jinja2.async_utils.auto_aiter(Broken())]
                return await MODULE.collect_iteration(Broken())
            except Exception as error:
                return type(error), str(error), error is failure
        for invalid in (False, True):
            expected = asyncio.run(run('reference', invalid))
            self.assertEqual(asyncio.run(run('native', invalid)), expected)

    def test_async_iteration_cancellation_cleanup(self):
        async def run(mode):
            trace = []
            async def values():
                try:
                    trace.append('start')
                    await asyncio.sleep(60)
                    yield 1
                except asyncio.CancelledError:
                    trace.append('caught')
                    await asyncio.sleep(0)
                    raise
                finally:
                    trace.append('finally')
            async def reference_values():
                return [value async for value in jinja2.async_utils.auto_aiter(values())]
            coroutine = reference_values() if mode == 'reference' else MODULE.collect_iteration(values())
            task = asyncio.create_task(coroutine)
            await asyncio.sleep(.01)
            task.cancel()
            try:
                await asyncio.wait_for(task, 1)
            except asyncio.CancelledError:
                trace.append('cancelled')
            return trace
        expected = asyncio.run(run('reference'))
        self.assertEqual(expected, ['start', 'caught', 'finally', 'cancelled'])
        self.assertEqual(asyncio.run(run('native')), expected)

    def test_foreign_loop_and_self_await_errors_enter_callback(self):
        async def run(mode, self_await):
            other = asyncio.new_event_loop()
            future = other.create_future()
            async def callback():
                try:
                    await (asyncio.current_task() if self_await else future)
                except RuntimeError as error:
                    return ('self' if 'Task cannot await on itself' in str(error)
                            else 'loop' if 'attached to a different loop' in str(error)
                            else str(error))
            try:
                coroutine = reference([callback]) if mode == 'reference' else getattr(MODULE, mode)([callback])
                return await asyncio.wait_for(coroutine, 1)
            finally:
                future.cancel()
                other.close()
        for self_await in (False, True):
            expected = asyncio.run(run('reference', self_await))
            self.assertEqual(expected, ['self' if self_await else 'loop'])
            for mode in ('run_direct', 'run_from_worker'):
                with self.subTest(mode=mode, self_await=self_await):
                    self.assertEqual(asyncio.run(run(mode, self_await)), expected)

    def test_bad_yield_is_thrown_into_callback(self):
        @types.coroutine
        def callback():
            try:
                yield 'bad'
            except RuntimeError as error:
                return str(error)
        async def check(mode):
            if mode == 'reference':
                return await reference([callback])
            return await getattr(MODULE, mode)([callback])
        expected = asyncio.run(check('reference'))
        self.assertEqual(expected, ["Task got bad yield: 'bad'"])
        for mode in ('run_direct', 'run_from_worker'):
            with self.subTest(mode=mode):
                self.assertEqual(asyncio.run(check(mode)), expected)

    def test_future_and_generator_yield_errors_enter_callback(self):
        async def run(mode, generator):
            future = asyncio.get_running_loop().create_future()
            @types.coroutine
            def callback():
                try:
                    yield (item for item in ()) if generator else future
                except RuntimeError as error:
                    text = str(error)
                    return ('generator' if 'yield was used instead of yield from for generator' in text
                            else 'future' if 'yield was used instead of yield from in task' in text
                            else text)
            try:
                coroutine = reference([callback]) if mode == 'reference' else getattr(MODULE, mode)([callback])
                return await asyncio.wait_for(coroutine, 1)
            finally:
                future.cancel()
        for generator in (False, True):
            expected = asyncio.run(run('reference', generator))
            self.assertEqual(expected, ['generator' if generator else 'future'])
            for mode in ('run_direct', 'run_from_worker'):
                with self.subTest(mode=mode, generator=generator):
                    self.assertEqual(asyncio.run(run(mode, generator)), expected)

    def test_missing_primitive_set_and_builtin_fallback(self):
        original = jinja2.async_utils._common_primitives
        marker = object()
        original_builtin = getattr(builtins, '_common_primitives', marker)
        async def check(mode, with_fallback):
            def callback():
                del jinja2.async_utils._common_primitives
                if with_fallback:
                    builtins._common_primitives = {int}
                return 1
            if mode == 'reference':
                return [await jinja2.async_utils.auto_await(callback())]
            return await getattr(MODULE, mode)([callback])
        try:
            if original_builtin is not marker:
                del builtins._common_primitives
            for mode in ('reference', 'run_direct', 'run_from_worker'):
                with self.subTest(mode=mode):
                    jinja2.async_utils._common_primitives = original
                    with self.assertRaisesRegex(NameError, "name '_common_primitives' is not defined"):
                        asyncio.run(check(mode, False))
                    jinja2.async_utils._common_primitives = original
                    self.assertEqual(asyncio.run(check(mode, True)), [1])
                    del builtins._common_primitives
        finally:
            jinja2.async_utils._common_primitives = original
            if original_builtin is not marker:
                builtins._common_primitives = original_builtin
            elif hasattr(builtins, '_common_primitives'):
                del builtins._common_primitives

    def test_dynamic_primitive_set_and_membership_error(self):
        class AwaitableList(list):
            def __await__(self):
                async def result():
                    return 'awaited'
                return result().__await__()

        original = jinja2.async_utils._common_primitives
        value = AwaitableList()
        async def check(mode):
            def include():
                jinja2.async_utils._common_primitives = set(original) | {AwaitableList}
                return value
            def exclude():
                jinja2.async_utils._common_primitives = set(original)
                return value
            if mode == 'reference':
                result = [await jinja2.async_utils.auto_await(callback()) for callback in (include, exclude)]
            else:
                result = await getattr(MODULE, mode)([include, exclude])
            self.assertIs(result[0], value)
            self.assertEqual(result[1], 'awaited')
            failure = ValueError('membership failure')
            class Broken:
                def __contains__(self, item):
                    raise failure
            jinja2.async_utils._common_primitives = Broken()
            try:
                if mode == 'reference':
                    await jinja2.async_utils.auto_await(1)
                else:
                    await getattr(MODULE, mode)([lambda: 1])
            except ValueError as error:
                self.assertIs(error, failure)
            else:
                self.fail('membership error not propagated')

        try:
            for mode in ('reference', 'run_direct', 'run_from_worker'):
                with self.subTest(mode=mode):
                    asyncio.run(check(mode))
        finally:
            jinja2.async_utils._common_primitives = original

    def test_exact_primitives_and_awaitable_subclasses(self):
        class AwaitableList(list):
            def __await__(self):
                async def result():
                    await asyncio.sleep(0)
                    return 'awaited'
                return result().__await__()

        class PlainInt(int):
            pass

        values = [None, True, 23, 1.25, 'text', [1], (2,), {'a': 3},
                  AwaitableList([4]), PlainInt(5), b'bytes']

        async def run():
            expected = [await jinja2.async_utils.auto_await(value) for value in values]
            for name in ('run_direct', 'run_from_worker'):
                actual = await getattr(MODULE, name)([lambda value=value: value for value in values])
                self.assertEqual(actual, expected)
                self.assertIs(actual[9], values[9])
        asyncio.run(run())

    def test_task_loop_context_future_and_generator(self):
        async def run(mode):
            state = contextvars.ContextVar('state', default='initial')
            trace, tasks, loops, threads = [], [], [], []
            def observe(label):
                trace.append(label)
                tasks.append(asyncio.current_task())
                loops.append(asyncio.get_running_loop())
                threads.append(threading.get_ident())
            async def first():
                observe('first:start');state.set('changed');await asyncio.sleep(0);observe('first:end');return 'A'
            def second():
                observe('second:'+state.get())
                future = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(future.set_result, 'B')
                return future
            @types.coroutine
            def third():
                observe('third:start');yield;observe('third:end');return 'C'
            callbacks = [first, second, third]
            result = await (MODULE.run_from_worker(callbacks) if mode == 'worker' else MODULE.run_direct(callbacks) if mode == 'direct' else reference(callbacks))
            return result, trace, len(set(map(id,tasks))), len(set(map(id,loops))), len(set(threads))
        expected = asyncio.run(run('reference'))
        self.assertEqual(expected, (['A','B','C'], ['first:start','first:end','second:changed','third:start','third:end'], 1, 1, 1))
        for mode in ('direct','worker'):
            with self.subTest(mode=mode):self.assertEqual(asyncio.run(run(mode)), expected)

    def test_exception_identity_and_no_later_callback(self):
        for mode in ('reference', 'direct', 'worker'):
            failure = ValueError('callback failure');calls=[]
            async def broken():
                await asyncio.sleep(0)
                raise failure
            async def run():
                callbacks = [broken, lambda:calls.append('later')]
                try:await (MODULE.run_from_worker(callbacks) if mode == 'worker' else MODULE.run_direct(callbacks) if mode == 'direct' else reference(callbacks))
                except ValueError as error:self.assertIs(error, failure)
                else:self.fail('exception not raised')
            asyncio.run(run())
            self.assertEqual(calls, [])

    def test_cancellation_and_async_cleanup(self):
        async def run(mode):
            trace=[];errors=[]
            async def callback():
                try:trace.append('start');await asyncio.sleep(60)
                except asyncio.CancelledError as error:
                    errors.append(error);trace.append('caught');await asyncio.sleep(0);raise
                finally:trace.append('finally')
            coroutine = MODULE.run_from_worker([callback]) if mode == 'worker' else MODULE.run_direct([callback]) if mode == 'direct' else reference([callback])
            task = asyncio.create_task(coroutine)
            await asyncio.sleep(.01);task.cancel()
            try:await asyncio.wait_for(task,1)
            except asyncio.CancelledError as error:errors.append(error);trace.append('cancelled')
            return trace, len(errors)==2 and errors[0] is errors[1]
        expected=asyncio.run(run('reference'))
        self.assertEqual(expected, (['start','caught','finally','cancelled'], True))
        for mode in ('direct','worker'):
            with self.subTest(mode=mode):self.assertEqual(asyncio.run(run(mode)), expected)

    def test_cancellation_before_worker_callback(self):
        async def run():
            calls=[]
            task=asyncio.create_task(MODULE.run_from_worker([lambda:calls.append('late')],delay_ms=200))
            await asyncio.sleep(.01);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,1)
            await asyncio.sleep(.25)
            self.assertEqual(calls, [])
        asyncio.run(run())

    def test_suppressed_cancellation(self):
        async def callback():
            try:await asyncio.sleep(60)
            except asyncio.CancelledError:await asyncio.sleep(0);return 'recovered'
        async def run():
            task=asyncio.create_task(MODULE.run_from_worker([callback]))
            await asyncio.sleep(.01);task.cancel()
            self.assertEqual(await asyncio.wait_for(task,1), ['recovered'])
        asyncio.run(run())

    def test_plain_await_iterator_and_concurrent_tasks(self):
        class Empty:
            def __await__(self):return iter(())
        async def run():
            self.assertEqual(await MODULE.run_from_worker([lambda:Empty()]), [None])
            async def one():
                task=asyncio.current_task()
                async def callback():
                    await asyncio.sleep(0)
                    self.assertIs(asyncio.current_task(), task)
                    return 42
                return await MODULE.run_from_worker([callback,callback])
            self.assertEqual(await asyncio.gather(*(one() for _ in range(50))), [[42,42]]*50)
        asyncio.run(run())


def main():
    global MODULE
    parser=argparse.ArgumentParser()
    parser.add_argument('--extension',required=True)
    args=parser.parse_args()
    spec=importlib.util.spec_from_file_location('_async_direct',args.extension)
    MODULE=importlib.util.module_from_spec(spec);spec.loader.exec_module(MODULE)
    unittest.main(argv=['test_async_direct'],verbosity=2)


if __name__=='__main__':main()
