"""Differential tests for section-type adjustment on the exposed graph."""
import importlib
import unittest

from docx import Document
from docx.enum.section import WD_SECTION
import docxtpl
import docxtpl_native


class NativeComposerSectionTests(unittest.TestCase):
    def test_real_document_sections(self):
        def run(engine, master_count, child_count):
            master=Document(); child=Document()
            for i in range(1, master_count):
                master.add_section((WD_SECTION.CONTINUOUS, WD_SECTION.NEW_PAGE)[i % 2])
            for i in range(1, child_count):
                child.add_section((WD_SECTION.NEW_PAGE, WD_SECTION.CONTINUOUS)[i % 2])
            cls=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            result=cls(master).fix_section_types(child)
            return result,[section.start_type for section in master.sections],[section.start_type for section in child.sections]
        for master_count,child_count in ((1,1),(1,3),(3,1),(3,2),(2,3)):
            with self.subTest(master=master_count,child=child_count):
                self.assertEqual(run(docxtpl_native,master_count,child_count),run(docxtpl,master_count,child_count))

    def test_access_order_and_partial_change_after_failure(self):
        def run(engine, fail=False, master_count=3, child_count=2):
            trace=[]; marker=RuntimeError('section read failed')
            class Section:
                def __init__(self, name): self.name=name;self.value=name
                @property
                def start_type(self):
                    trace.append(('get',self.name))
                    if fail and self.name=='child-1': raise marker
                    return self.value
                @start_type.setter
                def start_type(self,value): trace.append(('set',self.name,value));self.value=value
            class Sections:
                def __init__(self,name,count):self.name=name;self.items=[Section(f'{name}-{i}') for i in range(count)]
                def __len__(self):trace.append(('len',self.name));return len(self.items)
                def __getitem__(self,index):trace.append(('index',self.name,index));return self.items[index]
            class Part: package=object()
            class Doc:
                part=Part()
                def __init__(self,name,count):self.name=name;self._sections=Sections(name,count)
                @property
                def sections(self):trace.append(('sections',self.name));return self._sections
            master=Doc('master',master_count);child=Doc('child',child_count)
            cls=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            try: result=cls(master).fix_section_types(child); error=None
            except Exception as exc: result=None;error=(type(exc).__name__,exc.args,exc is marker)
            return result,error,trace,[item.value for item in master._sections.items]
        for fail,master_count,child_count in ((False,3,2),(True,3,2),(False,1,2),(False,2,1)):
            with self.subTest(fail=fail,master=master_count,child=child_count):
                self.assertEqual(run(docxtpl_native,fail,master_count,child_count),run(docxtpl,fail,master_count,child_count))


if __name__=='__main__': unittest.main()
