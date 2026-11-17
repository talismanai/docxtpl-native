"""Differential checks for concatenation evaluation and Python object protocols."""
import unittest

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from markupsafe import Markup


@unittest.skipUnless(docxtpl_native.capabilities()['native_available'], 'native extension unavailable')
class NativeConcatOrderTests(unittest.TestCase):
    def run_case(self, native, source, *, autoescape=False, broken=False):
        trace=[]
        failure=ValueError('conversion failed')
        class Value:
            def __init__(self, name):self.name=name
            def __str__(self):
                trace.append('str:'+self.name)
                if broken and self.name=='a':raise failure
                return '<'+self.name+'>'
        def get(name):trace.append('get:'+name);return Value(name)
        env=jinja2.Environment(autoescape=autoescape)
        context={'get':get,'safe':Markup('<safe>')}
        try:
            if native:
                result=docxtpl_native._extension().PythonRenderSession(BridgeConfig(env,False),context).render('case',source)
            else:result=env.from_string(source).render(context)
            return 'ok',result,trace
        except Exception as error:return 'error',error is failure,type(error).__name__,str(error),trace

    def test_order_and_parenthesized_subexpressions(self):
        for source in ('{{ get("a") ~ get("b") ~ get("c") }}',
                       '{{ (get("a") ~ get("b")) ~ get("c") }}',
                       '{{ get("a") ~ (get("b") ~ get("c")) }}'):
            for autoescape in (False,True):
                for broken in (False,True):
                    with self.subTest(source=source,autoescape=autoescape,broken=broken):
                        self.assertEqual(self.run_case(True,source,autoescape=autoescape,broken=broken),
                                         self.run_case(False,source,autoescape=autoescape,broken=broken))

    def test_markup_is_escaped_once_with_multiple_operands(self):
        for source in ('{{ get("a") ~ safe ~ get("b") }}',
                       '{{ safe ~ get("a") ~ get("b") }}'):
            for autoescape in (False,True):
                with self.subTest(source=source,autoescape=autoescape):
                    self.assertEqual(self.run_case(True,source,autoescape=autoescape),
                                     self.run_case(False,source,autoescape=autoescape))


if __name__ == '__main__':unittest.main()
