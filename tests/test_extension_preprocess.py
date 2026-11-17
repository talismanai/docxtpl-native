"""A narrow native bridge for plain text transformed by a user extension."""
import unittest
from unittest.mock import patch

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2.ext import Extension


class ExtensionPreprocessTests(unittest.TestCase):
    def run_plain(self, native, source='before'):
        events=[]
        class Replace(Extension):
            def preprocess(self, source, name, filename=None):
                events.append((source,name,filename))
                return source.replace('before','after')
        environment=jinja2.Environment(extensions=[Replace])
        if native:
            session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{})
            result=session.render('case',source)
        else:result=environment.from_string(source).render({})
        return result,events

    def test_plain_source_calls_user_hook_once(self):
        for source in ('before','Café before 🐍','', 'first\nsecond before'):
            with self.subTest(source=source):self.assertEqual(self.run_plain(True,source),self.run_plain(False,source))

    def test_multiple_hooks_follow_registration_order(self):
        def run(native):
            events=[]
            class First(Extension):
                def preprocess(self,source,name,filename=None):
                    events.append(('first',source,name,filename));return source.replace('before','after')
            class Second(Extension):
                def preprocess(self,source,name,filename=None):
                    events.append(('second',source,name,filename));return source+'!'
            environment=jinja2.Environment(extensions=[First,Second])
            if native:
                session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{})
                result=session.render('case','before')
            else:result=environment.from_string('before').render({})
            return result,events
        self.assertEqual(run(True),run(False))

    def test_multiple_hooks_follow_static_priorities_stably(self):
        def run(native):
            events=[]
            class Last(Extension):
                priority=200
                def preprocess(self,source,name,filename=None):events.append('last');return source+'L'
            class First(Extension):
                priority=10
                def preprocess(self,source,name,filename=None):events.append('first');return source+'F'
            class SamePriority(Extension):
                priority=10
                def preprocess(self,source,name,filename=None):events.append('same');return source+'S'
            environment=jinja2.Environment(extensions=[Last,First,SamePriority])
            if native:
                session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{})
                result=session.render('case','plain')
            else:result=environment.from_string('plain').render({})
            return result,events
        self.assertEqual(run(True),run(False))

    def test_instance_priority_overrides_class_priority(self):
        def run(native):
            events=[]
            class First(Extension):
                priority=1
                def preprocess(self,source,name,filename=None):events.append('first');return source
            class Second(Extension):
                priority=2
                def preprocess(self,source,name,filename=None):events.append('second');return source
            environment=jinja2.Environment(extensions=[First,Second])
            next(ext for ext in environment.extensions.values() if isinstance(ext,First)).priority=3
            if native:
                session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{})
                result=session.render('case','plain')
            else:result=environment.from_string('plain').render({})
            return result,events
        self.assertEqual(run(True),run(False))

    def test_multiple_hooks_keep_initial_snapshot_when_registry_changes(self):
        events=[]
        class Remove(Extension):
            def preprocess(self,source,name,filename=None):
                events.append('remove');self.environment.extensions.clear();return source
        class Next(Extension):
            def preprocess(self,source,name,filename=None):events.append('next');return source
        environment=jinja2.Environment(extensions=[Remove,Next])
        session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{})
        with self.assertRaises(docxtpl_native.UnsupportedFeature):session.render('case','plain')
        self.assertEqual(events,['remove','next'])

    def test_multiple_hooks_may_remove_intermediate_template_syntax(self):
        def run(native):
            events=[]
            class Generate(Extension):
                def preprocess(self,source,name,filename=None):events.append('generate');return '{{ value }}'
            class Remove(Extension):
                def preprocess(self,source,name,filename=None):events.append(source);return 'plain'
            environment=jinja2.Environment(extensions=[Generate,Remove])
            if native:
                session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{})
                result=session.render('case','before')
            else:result=environment.from_string('before').render({})
            return result,events
        self.assertEqual(run(True),run(False))

    def test_later_hook_exception_is_preserved(self):
        failure=ValueError('later hook failed');events=[]
        class First(Extension):
            def preprocess(self,source,name,filename=None):events.append('first');return source
        class Broken(Extension):
            def preprocess(self,source,name,filename=None):events.append('broken');raise failure
        environment=jinja2.Environment(extensions=[First,Broken])
        session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{})
        with self.assertRaises(ValueError) as actual:session.render('case','plain')
        self.assertIs(actual.exception,failure)
        self.assertEqual(events,['first','broken'])

    def test_template_source_compiles_after_one_hook_call(self):
        events=[]
        class Replace(Extension):
            def preprocess(self,source,name,filename=None):events.append(source);return source
        environment=jinja2.Environment(extensions=[Replace])
        session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{'value':'x'})
        self.assertEqual(session.render('case','{{ value }}'),'x')
        self.assertEqual(events,['{{ value }}'])

    def test_generated_template_syntax_is_rejected_without_replaying_hook(self):
        events=[]
        class Generate(Extension):
            def preprocess(self,source,name,filename=None):events.append(source);return '{{ value }}'
        environment=jinja2.Environment(extensions=[Generate])
        session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{'value':'x'})
        with self.assertRaises(docxtpl_native.UnsupportedFeature):session.render('case','plain')
        self.assertEqual(events,['plain'])

    def test_user_preprocess_exception_is_not_replaced(self):
        failure=ValueError('preprocess failure');events=[]
        class Broken(Extension):
            def preprocess(self,source,name,filename=None):events.append(source);raise failure
        environment=jinja2.Environment(extensions=[Broken])
        session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{})
        with self.assertRaises(ValueError) as actual:session.render('case','plain')
        self.assertIs(actual.exception,failure)
        self.assertEqual(events,['plain'])

    def test_hook_cannot_silently_change_template_syntax(self):
        events=[]
        class ChangeSyntax(Extension):
            def preprocess(self,source,name,filename=None):
                events.append(source)
                self.environment.variable_start_string='[['
                self.environment.variable_end_string=']]'
                return '[[ value ]]'
        environment=jinja2.Environment(extensions=[ChangeSyntax])
        session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{'value':'x'})
        with self.assertRaises(docxtpl_native.UnsupportedFeature):session.render('case','plain')
        self.assertEqual(events,['plain'])

    def test_hook_cannot_silently_install_token_filter(self):
        events=[]
        class ChangeFilter(Extension):
            def preprocess(self,source,name,filename=None):
                events.append(source)
                self.filter_stream=lambda stream:stream
                return source
        environment=jinja2.Environment(extensions=[ChangeFilter])
        session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{})
        with self.assertRaises(docxtpl_native.UnsupportedFeature):session.render('case','plain')
        self.assertEqual(events,['plain'])

    def test_monkeypatched_base_token_filter_is_not_skipped(self):
        events=[]
        class Replace(Extension):
            def preprocess(self,source,name,filename=None):events.append('preprocess');return source
        def filtered(self,stream):events.append('filter');return stream
        with patch.object(Extension,'filter_stream',filtered):
            environment=jinja2.Environment(extensions=[Replace])
            session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{})
            self.assertEqual(session.render('case','plain'),'plain')
        self.assertEqual(events,['preprocess','filter'])

    def test_spoofed_base_token_filter_is_not_skipped(self):
        events=[]
        class Replace(Extension):
            def preprocess(self,source,name,filename=None):events.append('preprocess');return source
        def filtered(self,stream):events.append('filter');return stream
        filtered.__module__='jinja2.ext'
        filtered.__qualname__='Extension.filter_stream'
        with patch.object(Extension,'filter_stream',filtered):
            environment=jinja2.Environment(extensions=[Replace])
            session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{})
            self.assertEqual(session.render('case','plain'),'plain')
        self.assertEqual(events,['preprocess','filter'])

    def test_spoofed_identity_with_required_argument_is_not_skipped(self):
        class Replace(Extension):
            def preprocess(self,source,name,filename=None):return source
        def filtered(self,stream,*,required):return stream
        filtered.__module__='jinja2.ext'
        filtered.__qualname__='Extension.filter_stream'
        with patch.object(Extension,'filter_stream',filtered):
            environment=jinja2.Environment(extensions=[Replace])
            session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{})
            with self.assertRaises(TypeError):session.render('case','plain')

    def test_priority_descriptor_is_left_to_reference_parser(self):
        def run(native):
            events=[]
            class DynamicPriority(Extension):
                @property
                def priority(self):events.append('priority');return 100
                def preprocess(self,source,name,filename=None):events.append('preprocess');return source
            environment=jinja2.Environment(extensions=[DynamicPriority])
            if native:
                session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{})
                self.assertEqual(session.render('case','plain'),'plain')
            else:environment.parse('plain')
            return events
        self.assertEqual(run(True),run(False))

    def test_priority_descriptor_cannot_be_hidden_by_instance_dictionary(self):
        def run(native):
            events=[]
            class DynamicPriority(Extension):
                @property
                def priority(self):events.append('priority');return 100
                def preprocess(self,source,name,filename=None):events.append('preprocess');return source
            environment=jinja2.Environment(extensions=[DynamicPriority])
            next(iter(environment.extensions.values())).__dict__['priority']=1
            if native:
                session=docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment,False),{})
                self.assertEqual(session.render('case','plain'),'plain')
            else:environment.parse('plain')
            return events
        self.assertEqual(run(True),run(False))


if __name__=='__main__':unittest.main()
