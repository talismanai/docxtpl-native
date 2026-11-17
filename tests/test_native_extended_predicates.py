"""Native predicate bodies share object operators and exception handling."""
from contextlib import contextmanager
from unittest.mock import patch
import sys
import unittest
import docxtpl, docxtpl_native
import jinja2.tests as predicates
from jinja2.defaults import DEFAULT_TESTS
from paritylab.compare import compare_docx
from paritylab.extended_predicate_probes import PROBES, NAMES
from test_native_predicate_keywords import run


@contextmanager
def native_extended_only():
    codes = {DEFAULT_TESTS[name].__code__ for name in NAMES}
    previous = sys.getprofile()
    def profile(frame, event, argument):
        if event == 'call' and frame.f_code in codes:
            raise AssertionError('standard Python predicate executed: '+frame.f_code.co_name)
    sys.setprofile(profile)
    try: yield
    finally: sys.setprofile(previous)


class ExtendedPredicateTests(unittest.TestCase):
    def check(self, execute):
        expected, reference = execute(docxtpl)
        with native_extended_only(): actual, native = execute(docxtpl_native)
        self.assertEqual(actual, expected)
        self.assertEqual(reference is None, native is None)
        if reference is not None:
            self.assertTrue(compare_docx(reference, native)['equivalent'])
        return expected

    def test_literal_results_and_native_execution(self):
        probe = PROBES['native_predicate_extended']
        reference = probe.execute(docxtpl)
        self.assertEqual(reference.observation, probe.expected)
        with native_extended_only(): native = probe.execute(docxtpl_native)
        self.assertEqual(native.observation, probe.expected)
        self.assertTrue(compare_docx(reference.outputs['document.docx'],native.outputs['document.docx'])['equivalent'])

    def test_numeric_protocol_order_return_identity_and_errors(self):
        for name in ('odd', 'even', 'divisibleby'):
            for phase in ('ok', 'mod', 'eq'):
                def execute(engine):
                    trace=[]; failure=ValueError('numeric predicate failed'); marker=object()
                    class Remainder:
                        def __eq__(self, other):
                            trace.append(['eq',other])
                            if phase=='eq': raise failure
                            return marker
                    class Value:
                        def __mod__(self, other):
                            trace.append(['mod',other])
                            if phase=='mod': raise failure
                            return Remainder()
                    def capture(value):
                        trace.append(['identity',value is marker]);return 'done'
                    expression='value is '+name+('(divisor)' if name=='divisibleby' else '')
                    return run(engine,'{{ capture('+expression+') }}',{'value':Value(),'divisor':3,'capture':capture},trace,failure=failure)
                with self.subTest(name=name,phase=phase):
                    observation=self.check(execute)
                    divisor=3 if name=='divisibleby' else 2
                    expected=[['mod',divisor]]
                    if phase!='mod':expected.append(['eq',1 if name=='odd' else 0])
                    if phase=='ok':expected.append(['identity',True])
                    self.assertEqual(observation['trace'],expected)
                    self.assertEqual(observation['error'],None if phase=='ok' else ['ValueError','numeric predicate failed',True])

    def test_string_conversion_method_and_return_identity(self):
        for name in ('lower','upper'):
            def execute(engine):
                trace=[]; marker=object()
                class Converted:
                    def islower(self):trace.append('lower');return marker
                    def isupper(self):trace.append('upper');return marker
                def convert(value):trace.append('str');return Converted()
                def capture(value):trace.append(value is marker);return 'done'
                with patch.object(predicates,'str',convert,create=True):
                    return run(engine,'{{ capture(value is '+name+') }}',{'value':41,'capture':capture},trace)
            with self.subTest(name=name):self.assertEqual(self.check(execute)['trace'],['str',name,True])

    def test_mapping_and_number_resolve_callable_before_live_class(self):
        for name in ('mapping','number'):
            def execute(engine):
                trace=[]; marker=object(); alias=object()
                def changed(value,kind):raise AssertionError('callable resolved after class')
                class Classes:
                    @property
                    def Mapping(self):
                        trace.append('class');predicates.isinstance=changed;return alias
                def check(value,kind):trace.append(['check',kind is alias]);return marker
                def capture(value):trace.append(['identity',value is marker]);return 'done'
                with patch.object(predicates,'isinstance',check,create=True), \
                     patch.object(predicates,'abc' if name=='mapping' else 'Number',Classes() if name=='mapping' else alias,create=True):
                    return run(engine,'{{ capture(value is '+name+') }}',{'value':41,'capture':capture},trace)
            with self.subTest(name=name):
                self.assertEqual(self.check(execute)['trace'],(['class'] if name=='mapping' else [])+[['check',True],['identity',True]])

    def test_sequence_and_iterable_handlers_and_cleanup(self):
        for name in ('sequence','iterable'):
            for phase in ('ok','type','value','base','rebound'):
                def execute(engine):
                    trace=[]
                    error_type={'type':TypeError,'value':ValueError,'base':BaseException,'rebound':ValueError}.get(phase)
                    failure=error_type('protocol failed') if error_type else None
                    class Temporary:
                        def __del__(self):trace.append('release')
                    class Value:
                        def __getattribute__(self,name):
                            if name=='__getitem__':trace.append('getitem');return Temporary()
                            return object.__getattribute__(self,name)
                    def operation(value):
                        trace.append('len' if name=='sequence' else 'iter')
                        if failure:raise failure
                        return Temporary()
                    exception_name='Exception' if name=='sequence' else 'TypeError'
                    with patch.object(predicates,'len' if name=='sequence' else 'iter',operation,create=True), \
                         patch.object(predicates,exception_name,ValueError if phase=='rebound' else getattr(__import__('builtins'),exception_name),create=True):
                        return run(engine,'{{ value is '+name+' }}',{'value':Value()},trace,failure=failure)
                with self.subTest(name=name,phase=phase):
                    observation=self.check(execute)
                    if phase=='ok':self.assertEqual(observation['trace'],['len','release','getitem','release'] if name=='sequence' else ['iter','release'])
                    elif phase=='base' or (phase=='value' and name=='iterable'):
                        self.assertTrue(observation['error'][2])
                    else:self.assertIsNone(observation['error'])

    def test_membership_truth_and_errors(self):
        for fails in (False,True):
            def execute(engine):
                trace=[];failure=ValueError('truth failed')
                class Verdict:
                    def __bool__(self):
                        trace.append('truth')
                        if fails:raise failure
                        return True
                class Container:
                    def __contains__(self,value):trace.append(['contains',value]);return Verdict()
                return run(engine,'{{ value is in(seq) }}',{'value':41,'seq':Container()},trace,failure=failure)
            with self.subTest(fails=fails):self.assertEqual(self.check(execute)['trace'],[['contains',41],'truth'])

    def test_escaped_live_hasattr_returns_original_object(self):
        def execute(engine):
            trace=[];marker=object()
            def check(value,name):trace.append(name);return marker
            def capture(value):trace.append(value is marker);return 'done'
            with patch.object(predicates,'hasattr',check,create=True):
                return run(engine,'{{ capture(value is escaped) }}',{'value':41,'capture':capture},trace)
        self.assertEqual(self.check(execute)['trace'],['__html__',True])


if __name__=='__main__':unittest.main()
