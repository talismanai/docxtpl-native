"""The native live-part bridge follows the former Python protocol order."""
import array
import re
import unittest
from typing import Any
from unittest.mock import patch

import docxtpl
import docxtpl_native


def reference(document: Any, session: Any, source: Any, part: Any) -> str:
    document.current_rendering_part=part
    source=re.sub(r'<w:p([ >])',r'\n<w:p\1',document.patch_xml(source))
    rendered=session.render(str(part.partname).lstrip('/'),source)
    rendered=re.sub(r'\n<w:p([ >])',r'<w:p\1',rendered)
    for initial,result in (('{_{','{{'),('}_}','}}'),('{_%','{%'),('%_}','%}')):
        rendered=rendered.replace(initial,result)
    return docxtpl.DocxTemplate.resolve_listing(document, rendered)


class NativeLiveXmlTests(unittest.TestCase):
    def test_reference_listing_is_independent_and_mutations_are_rejected(self) -> None:
        extension = docxtpl_native._extension()
        with patch.object(extension, 'resolve_listing_xml', side_effect=AssertionError('native oracle')):
            self.assertEqual(self.observe(reference, 'plain', '<w:p>\ud800</w:p>')[0], 'ok')

        original = extension.render_live_xml

        def mutated(document: Any, session: Any, source: Any, part: Any) -> str:
            return original(document, session, source, part) + 'MUTATED'

        with patch.object(extension, 'render_live_xml', mutated), self.assertRaises(AssertionError):
            self.compare('plain', '<w:p>\ud800</w:p>')

    def observe(self,method,source,output,*,patch_failure=False,render_failure=False):
        events=[];failure=ValueError('original failure')
        class Template:
            def __setattr__(self,name,value):
                if name=='current_rendering_part':events.append('set_part')
                object.__setattr__(self,name,value)
            def patch_xml(self,value):
                events.append(('patch',value is source))
                if patch_failure:raise failure
                return value
        class Part:
            @property
            def partname(self):events.append('partname');return '/word/document.xml'
        class Session:
            def __getattribute__(self,name):
                if name=='render':events.append('lookup_render')
                return object.__getattribute__(self,name)
            def render(self,name,value):
                events.append(('render',name,value,value is source))
                if render_failure:raise failure
                return output
        document=Template();session=Session();part=Part()
        try:
            value=method(document,session,source,part)
            return ('ok',value,events,document.current_rendering_part is part)
        except Exception as error:
            return ('error',type(error),str(error),error is failure,events,document.current_rendering_part is part)

    def compare(self,source,output,**kwargs):
        expected=self.observe(reference,source,output,**kwargs)
        actual=self.observe(docxtpl_native._extension().render_live_xml,source,output,**kwargs)
        self.assertEqual(actual,expected)

    def test_transformations_and_call_order(self):
        for source,output in [('plain','<w:p>before</w:p>'),
                              ('<w:p x="1">a</w:p><w:p>b</w:p>','\n<w:p>{_{_%|{_%_}|%_}_}</w:p>'),
                              ('<w:proofErr/>','before\n<w:p>x</w:p>')]:
            with self.subTest(source=source,output=output):self.compare(source,output)

    def test_unpaired_surrogates_and_subclasses(self):
        class Text(str):pass
        for source,output in [('<w:p>\ud800</w:p>','normal'),('plain','\n<w:p>\ud800{_{_%</w:p>'),
                              (Text('plain'),Text('<w:p>value</w:p>')),
                              (Text('plain\ud800'), 'normal')]:
            with self.subTest(source=repr(source),output=repr(output)):self.compare(source,output)

    def test_non_string_patch_results_match_regex_errors(self):
        for value in (None,1,b'x',bytearray(b'x'),memoryview(b'x'),array.array('i',[1]),[]):
            with self.subTest(value=repr(value)):self.compare(value,'ignored')

    def test_non_string_render_results_match_regex_errors(self):
        for value in (None,1,b'x',bytearray(b'x'),memoryview(b'x'),array.array('i',[1]),[]):
            with self.subTest(value=repr(value)):self.compare('source',value)

    def test_original_exceptions_and_partial_state(self):
        self.compare('source','output',patch_failure=True)
        self.compare('source','output',render_failure=True)

    def test_partname_string_conversion_respects_globals(self):
        original=str
        def stringify(value):return '/custom/'+original(value)
        with patch.dict(globals(),{'str':stringify}):
            self.compare('source','output')


if __name__=='__main__':unittest.main()
