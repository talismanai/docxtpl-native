"""Differential checks for lazy filtered-loop lookahead and error timing."""
import unittest

import jinja2
import docxtpl_native
from docxtpl_native.bridge import BridgeConfig


class FilteredMetadataTests(unittest.TestCase):
    def observe(self, native, source, *, failure_at=None):
        events = []
        error = RuntimeError('predicate failed')
        env = jinja2.Environment(extensions=['jinja2.ext.loopcontrols'])
        def selected(value):
            events.append(('filter', value))
            if value == failure_at: raise error
            return value % 2 == 1
        def emit(value): events.append(('emit', value)); return value
        env.tests['selected'] = selected
        context = {'values': [1, 2, 3, 4, 5], 'emit': emit}
        try:
            if native:
                text = docxtpl_native._extension().PythonRenderSession(BridgeConfig(env, False), context).render('case', source)
            else:
                text = env.from_string(source).render(context)
            result = ('ok', text)
        except Exception as actual:
            result = (type(actual).__name__, str(actual), actual is error)
        return result, events

    def test_repeated_lookahead_and_length_use_only_filtered_items(self):
        sources = [
            "{% for x in values if x is selected %}{{ emit(x) }}:{{ loop.last }}:{{ loop.last }}:{{ loop.index }};{% endfor %}",
            "{% for x in values if x is selected %}{{ emit(x) }}:{{ loop.nextitem|default('end') }}:{{ loop.length }}:{{ loop.revindex }};{% endfor %}",
            "{% for x in values if x is selected %}{{ loop.length }}:{{ emit(x) }}:{{ loop.previtem|default('start') }};{% endfor %}",
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(self.observe(True, source), self.observe(False, source))

    def test_predicate_failure_occurs_after_prior_body_and_at_lookahead(self):
        source = "{% for x in values if x is selected %}{{ emit(x) }}:{{ loop.last }};{% endfor %}"
        for failure_at in (1, 2, 3, 4, 5):
            with self.subTest(failure_at=failure_at):
                self.assertEqual(self.observe(True, source, failure_at=failure_at),
                                 self.observe(False, source, failure_at=failure_at))

    def test_break_does_not_consume_future_values_without_lookahead(self):
        source = "{% for x in values if x is selected %}{{ emit(x) }}{% if loop.index == 1 %}{% break %}{% endif %}{{ loop.last }}{% endfor %}"
        self.assertEqual(self.observe(True, source), self.observe(False, source))

    def test_list_mutation_before_first_lookahead(self):
        def run(native, field):
            events = []; values = [1, 2]; env = jinja2.Environment()
            def selected(value): events.append(('filter', value)); return value % 2 == 1
            def emit(value):
                events.append(('emit', value))
                if value == 1: values.append(3)
                return value
            env.tests['selected'] = selected
            source = "{% for x in values if x is selected %}{{ emit(x) }}:{{ loop."+field+" }};{% endfor %}"
            context = {'values': values, 'emit': emit}
            if native:
                text = docxtpl_native._extension().PythonRenderSession(BridgeConfig(env, False), context).render('case', source)
            else:
                text = env.from_string(source).render(context)
            return text, events, values
        for field in ('last', 'length'):
            with self.subTest(field=field): self.assertEqual(run(True, field), run(False, field))

    def test_predicate_callback_sees_outer_context_not_loop_target(self):
        def run(native, include_outer, as_function):
            events = []; env = jinja2.Environment()
            @jinja2.pass_context
            def selected(context, value):
                events.append((value, context.get('x', 'missing'), 'loop' in context))
                return value % 2 == 1
            env.tests['selected'] = selected
            predicate = 'selected(x)' if as_function else 'x is selected'
            source = "{% for x in values if "+predicate+" %}{{ x }}:{{ loop.last }};{% endfor %}"
            context = {'values': [1, 2, 3]}
            if as_function: context['selected'] = selected
            if include_outer: context['x'] = 'outer'
            if native:
                text = docxtpl_native._extension().PythonRenderSession(BridgeConfig(env, False), context).render('case', source)
            else:
                text = env.from_string(source).render(context)
            return text, events
        for include_outer in (False, True):
            for as_function in (False, True):
                with self.subTest(include_outer=include_outer, as_function=as_function):
                    self.assertEqual(run(True, include_outer, as_function), run(False, include_outer, as_function))

    def test_lookahead_inside_with_uses_context_at_loop_creation(self):
        def run(native):
            events = []; env = jinja2.Environment()
            @jinja2.pass_context
            def selected(context, value):
                events.append((value, context.get('item', 'missing')))
                return value % 2 == 1
            env.tests['selected'] = selected
            source = "{% for x in values if x is selected %}{% with item='inner' %}{{ x }}:{{ loop.last }};{% endwith %}{% endfor %}"
            context = {'values': [1, 2, 3], 'item': 'outer'}
            if native:
                text = docxtpl_native._extension().PythonRenderSession(BridgeConfig(env, False), context).render('case', source)
            else:
                text = env.from_string(source).render(context)
            return text, events
        self.assertEqual(run(True), run(False))

    def test_nested_filter_callback_does_not_see_outer_loop_local(self):
        def run(native):
            events = []; env = jinja2.Environment()
            @jinja2.pass_context
            def selected(context, value):
                events.append((value, context.get('group', 'missing')))
                return value % 2 == 1
            env.tests['selected'] = selected
            source = "{% for group in groups %}{% for x in group if x is selected %}{{ x }}:{{ loop.last }};{% endfor %}{% endfor %}"
            context = {'groups': [[1, 2, 3]]}
            if native:
                text = docxtpl_native._extension().PythonRenderSession(BridgeConfig(env, False), context).render('case', source)
            else:
                text = env.from_string(source).render(context)
            return text, events
        self.assertEqual(run(True), run(False))

    def test_filter_result_truth_protocol_and_exception_identity(self):
        def run(native, fail_on):
            events = []; failure = RuntimeError('truth failed'); env = jinja2.Environment()
            class Truth:
                def __init__(self, value): self.value = value
                def __bool__(self):
                    events.append(('bool', self.value))
                    if self.value == fail_on: raise failure
                    return self.value % 2 == 1
            def selected(value): events.append(('filter', value)); return Truth(value)
            def emit(value): events.append(('emit', value)); return value
            env.tests['selected'] = selected
            source = "{% for x in values if x is selected %}{{ emit(x) }}:{{ loop.last }};{% endfor %}"
            context = {'values': [1, 2, 3], 'emit': emit}
            try:
                if native:
                    text = docxtpl_native._extension().PythonRenderSession(BridgeConfig(env, False), context).render('case', source)
                else:
                    text = env.from_string(source).render(context)
                result = ('ok', text)
            except Exception as actual:
                result = (type(actual).__name__, str(actual), actual is failure)
            return result, events
        for fail_on in (None, 1, 2, 3):
            with self.subTest(fail_on=fail_on): self.assertEqual(run(True, fail_on), run(False, fail_on))


if __name__ == '__main__': unittest.main()
