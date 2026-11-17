"""Differential checks for compile-time filter effects in call arguments."""
import unittest
import json
import os
from pathlib import Path
import subprocess
import sys

import jinja2
import docxtpl_native
from markupsafe import Markup
from docxtpl_native.bridge import BridgeConfig


def observe_hash_constants(native):
    results = []
    for count in (0, 1, 2, 3, 4, 8, 10, 100):
        for shape in ('set', 'frozen', 'nested'):
            original = {'x', 'y'} if count == 2 else {str(i) for i in range(count)}
            value = (frozenset(original) if shape == 'frozen' else
                     [original, {'frozen': frozenset(original)}] if shape == 'nested' else original)
            events = []
            environment = jinja2.Environment()
            def mark(item): events.append('filter'); return value
            def runtime(): events.append('runtime'); original.add('late mutation'); return ''
            def consume(item): events.append(repr(item)); return 'result'
            environment.filters['mark'] = mark
            source = "{{ runtime() }}{{ consume('x'|mark) }}"
            context = {'runtime': runtime, 'consume': consume}
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
                text = session.render('case', source)
            else:
                text = environment.from_string(source).render(context)
            results.append([count, shape, text, events])
    return results


class ConstantArgumentTests(unittest.TestCase):
    def test_hash_constants_in_independent_interpreters(self):
        for seed in (0, 3, 17, 101):
            environment = dict(os.environ, PYTHONHASHSEED=str(seed))
            environment['PYTHONPATH'] = str(Path(__file__).parent) + os.pathsep + environment.get('PYTHONPATH', '')
            observations = []
            for native in (False, True):
                code = ('import json; from test_constant_arguments import observe_hash_constants; '
                        f'print(json.dumps(observe_hash_constants({native})))')
                run = subprocess.run([sys.executable, '-c', code], env=environment,
                                     capture_output=True, text=True, timeout=60)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                observations.append(json.loads(run.stdout))
            self.assertEqual(len(observations[0]), len(observations[1]))
            for reference, actual in zip(*observations):
                with self.subTest(seed=seed, count=reference[0], shape=reference[1]):
                    self.assertEqual(actual, reference)

    def test_constant_hash_containers_preserve_compiled_order_across_hash_seeds(self):
        code = '''
import json
from test_constant_arguments import observe_hash_constants
actual=observe_hash_constants(True)
expected=observe_hash_constants(False)
if actual!=expected:
    print(json.dumps([{'actual': a, 'expected': e} for a,e in zip(actual,expected) if a!=e],ensure_ascii=False))
    raise SystemExit(1)
'''
        for seed in (0, 3, 17, 101):
            environment = dict(os.environ, PYTHONHASHSEED=str(seed))
            environment['PYTHONPATH'] = str(Path(__file__).parent) + os.pathsep + environment.get('PYTHONPATH', '')
            with self.subTest(seed=seed):
                run = subprocess.run([sys.executable, '-c', code], env=environment,
                                     capture_output=True, text=True, timeout=60)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def observe(self, native, *, result='string', optimized=True, source=None, fail=False):
        events = []
        marker = object()
        error = ValueError('filter failed')
        environment = jinja2.Environment(optimized=optimized)
        def mark(value):
            events.append('filter')
            if fail: raise error
            if result == 'list': return [value, {'nested': [value]}]
            if result == 'tuple': return (value, [value])
            if result == 'dict': return {'x': [value]}
            if result == 'opaque': return marker
            if result == 'set': return {'x', 'y'}
            if result == 'frozen': return frozenset({'x', 'y'})
            if result == 'range': return range(2, 9, 2)
            if result == 'complex': return complex(1, 2)
            if result == 'markup': return Markup('<b>x</b>')
            if result == 'large_int': return 2**200
            if result == 'nan': return float('nan')
            if result == 'inf': return float('inf')
            return value
        def consume(value):
            if result == 'opaque': events.append(('identity', value is marker))
            elif result == 'list':
                events.append(('list', repr(value))); value[1]['nested'].append('changed')
            else: events.append(('value', repr(value)))
            return 'result'
        def runtime(): events.append('runtime'); return ''
        environment.filters['mark'] = mark
        source = source or "{{ runtime() }}{{ consume('x'|mark) }}"
        context = {'runtime': runtime, 'consume': consume}
        try:
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
                text = session.render('case', source)
            else:
                text = environment.from_string(source).render(context)
            outcome = ('ok', text)
        except Exception as actual:
            outcome = (type(actual).__name__, str(actual), actual is error)
        return outcome, events

    def test_filter_effects_and_fresh_container_literals(self):
        for result in ('string', 'list', 'tuple', 'dict', 'opaque', 'set', 'frozen', 'range', 'complex', 'markup', 'large_int', 'nan', 'inf'):
            for optimized in (True, False):
                with self.subTest(result=result, optimized=optimized):
                    self.assertEqual(self.observe(True, result=result, optimized=optimized),
                                     self.observe(False, result=result, optimized=optimized))

    def test_filter_failure_preserves_exception_and_order(self):
        for optimized in (True, False):
            with self.subTest(optimized=optimized):
                self.assertEqual(self.observe(True, optimized=optimized, fail=True),
                                 self.observe(False, optimized=optimized, fail=True))

    def test_multiple_invocations_do_not_share_mutated_literal(self):
        source = "{{ consume('x'|mark) }}{{ consume('x'|mark) }}"
        self.assertEqual(self.observe(True, result='list', source=source),
                         self.observe(False, result='list', source=source))

    def test_decorated_filters_receive_compile_environment_and_escape_state(self):
        def run(native, decoration, autoescape):
            events = []
            env = jinja2.Environment(autoescape=autoescape)
            if decoration == 'environment':
                @jinja2.pass_environment
                def mark(actual, value):
                    events.append(('mark', actual is env)); return value
            elif decoration == 'eval_context':
                @jinja2.pass_eval_context
                def mark(actual, value):
                    events.append(('mark', actual.autoescape)); return value
            else:
                @jinja2.pass_context
                def mark(actual, value):
                    events.append(('mark', actual.eval_ctx.autoescape)); return value
            env.filters['mark'] = mark
            context = {'runtime': lambda: events.append('runtime') or '', 'consume': lambda x: x}
            source = "{{ runtime() }}{{ consume('x'|mark) }}"
            if native:
                text = docxtpl_native._extension().PythonRenderSession(BridgeConfig(env, False), context).render('case', source)
            else:
                text = env.from_string(source).render(context)
            return text, events
        for decoration in ('environment', 'eval_context', 'context'):
            for autoescape in (False, True):
                with self.subTest(decoration=decoration, autoescape=autoescape):
                    self.assertEqual(run(True, decoration, autoescape), run(False, decoration, autoescape))

    def test_optimization_setting_is_captured_before_filter_callbacks(self):
        def run(native):
            events = []
            env = jinja2.Environment()
            def mark(value):
                events.append('mark:' + value)
                env.optimized = False
                return value
            env.filters['mark'] = mark
            context = {'runtime': lambda: events.append('runtime') or '', 'consume': lambda x: x}
            source = "{{ runtime() }}{{ consume('x'|mark) }}{{ consume('y'|mark) }}"
            if native:
                text = docxtpl_native._extension().PythonRenderSession(BridgeConfig(env, False), context).render('case', source)
            else:
                text = env.from_string(source).render(context)
            return text, events
        self.assertEqual(run(True), run(False))

    def test_failed_first_attempt_can_succeed_on_retry(self):
        def run(native):
            events = []
            env = jinja2.Environment()
            def mark(value):
                events.append('filter')
                if len(events) == 1: raise ValueError('retry')
                return value
            env.filters['mark'] = mark
            context = {'runtime': lambda: events.append('runtime') or '', 'consume': lambda x: x}
            source = "{{ runtime() }}{{ consume('x'|mark) }}"
            if native:
                text = docxtpl_native._extension().PythonRenderSession(BridgeConfig(env, False), context).render('case', source)
            else:
                text = env.from_string(source).render(context)
            return text, events
        self.assertEqual(run(True), run(False))

    def test_compile_abort_is_not_retried_or_replaced(self):
        def run(native):
            events = []
            failure = KeyboardInterrupt('abort')
            env = jinja2.Environment()
            def mark(value): events.append('filter'); raise failure
            env.filters['mark'] = mark
            source = "{{ consume('x'|mark) }}"
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(BridgeConfig(env, False), {'consume': lambda x: x}).render('case', source)
                else:
                    env.from_string(source).render(consume=lambda x: x)
            except KeyboardInterrupt as actual:
                return actual is failure, str(actual), events
            self.fail('expected KeyboardInterrupt')
        self.assertEqual(run(True), run(False))

    def test_cyclic_literal_raises_during_compilation(self):
        def run(native):
            events = []
            cycle = []; cycle.append(cycle)
            env = jinja2.Environment()
            env.filters['mark'] = lambda value: events.append('filter') or cycle
            source = "{{ consume('x'|mark) }}"
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(BridgeConfig(env, False), {'consume': lambda x: x}).render('case', source)
                else:
                    env.from_string(source).render(consume=lambda x: x)
            except RecursionError as error:
                return str(error), events
            self.fail('expected RecursionError')
        self.assertEqual(run(True), run(False))

    def test_context_filter_retries_compile_lookup_without_calling_filter(self):
        def run(native):
            events = []
            class ContextFilter:
                jinja_pass_arg = jinja2.utils._PassArg.context
                def __getattribute__(self, name):
                    if name == 'jinja_pass_arg': events.append('marker')
                    return object.__getattribute__(self, name)
                def __call__(self, context, value):
                    events.append('filter'); return value
            env = jinja2.Environment(); env.filters['mark'] = ContextFilter()
            source = "{{ runtime() }}{{ consume('x'|mark) }}"
            context = {'runtime': lambda: events.append('runtime') or '', 'consume': lambda x: x}
            if native:
                text = docxtpl_native._extension().PythonRenderSession(BridgeConfig(env, False), context).render('case', source)
            else:
                text = env.from_string(source).render(context)
            return text, events
        self.assertEqual(run(True), run(False))

    def test_context_filter_uses_marker_from_compilation(self):
        def run(native):
            events = []
            env = jinja2.Environment()
            @jinja2.pass_context
            def mark(context, value): events.append(('filter', context['item'])); return value
            env.filters['mark'] = mark
            def runtime():
                events.append('runtime')
                mark.jinja_pass_arg = jinja2.utils._PassArg.environment
                return ''
            source = "{{ runtime() }}{{ consume('x'|mark) }}"
            context = {'runtime': runtime, 'consume': lambda x: x, 'item': 'visible'}
            if native:
                text = docxtpl_native._extension().PythonRenderSession(BridgeConfig(env, False), context).render('case', source)
            else:
                text = env.from_string(source).render(context)
            return text, events
        self.assertEqual(run(True), run(False))


if __name__ == '__main__': unittest.main()
