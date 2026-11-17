"""Referenced relationships on the caller-visible document model."""
import importlib
import unittest

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from lxml import etree
import docxtpl
import docxtpl_native

R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'


class NativeComposerReferencesTests(unittest.TestCase):
    def test_real_hyperlink_and_ignored_image(self):
        def run(engine):
            master=Document();child=Document()
            master.part.relate_to('https://occupied.invalid',RT.HYPERLINK,is_external=True)
            link=child.part.relate_to('https://target.invalid',RT.HYPERLINK,is_external=True)
            image=child.part.relate_to('https://image.invalid',RT.IMAGE,is_external=True)
            root=etree.fromstring(f'<root xmlns:r="{R}"><link r:id="{link}"/><image r:id="{image}"/></root>')
            cls=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            trace=[]
            class Custom(cls):
                def add_relationship(self,src,dst,relationship):
                    trace.append((src is child.part,dst is master.part,relationship.target_ref))
                    new=dst.rels.get_or_add_ext_rel(relationship.reltype,relationship.target_ref)
                    return dst.rels[new]
            result=Custom(master).add_referenced_parts(child.part,master.part,root)
            new_id=root[0].get(f'{{{R}}}id');rel=master.part.rels[new_id]
            return result,trace,new_id,rel.reltype,rel.target_ref,root[1].get(f'{{{R}}}id')==image
        actual=run(docxtpl_native)
        self.assertEqual(actual,run(docxtpl));self.assertEqual(actual[1],[(True,True,'https://target.invalid')]);self.assertTrue(actual[-1])

    def test_failure_keeps_earlier_mutation(self):
        def run(engine):
            master=Document();child=Document();marker=RuntimeError('second relation failed');trace=[]
            ids=[child.part.relate_to(f'https://{number}.invalid',RT.HYPERLINK,is_external=True) for number in (1,2)]
            root=etree.fromstring(f'<root xmlns:r="{R}"><link r:id="{ids[0]}"/><link r:id="{ids[1]}"/></root>')
            cls=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            class NewRel:rId='changed'
            class Custom(cls):
                def add_relationship(self,src,dst,rel):
                    trace.append(rel.target_ref)
                    if len(trace)==2:raise marker
                    return NewRel()
            try:result=Custom(master).add_referenced_parts(child.part,master.part,root);error=None
            except Exception as exc:result=None;error=(type(exc).__name__,exc.args,exc is marker)
            return result,error,trace,[node.get(f'{{{R}}}id') for node in root]
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_no_references_requires_no_relationship_override(self):
        def run(engine):
            master=Document();child=Document();root=etree.fromstring('<root><text/></root>')
            return importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master).add_referenced_parts(child.part,master.part,root)
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_real_external_relationship_without_override(self):
        def run(engine):
            master=Document();child=Document()
            master.part.relate_to('https://occupied.invalid',RT.HYPERLINK,is_external=True)
            old=child.part.relate_to('https://target.invalid',RT.HYPERLINK,is_external=True)
            root=etree.fromstring(f'<root xmlns:r="{R}"><link r:id="{old}"/><link r:id="{old}"/></root>')
            composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            result=composer.add_referenced_parts(child.part,master.part,root)
            ids=[node.get(f'{{{R}}}id') for node in root]
            rel=master.part.rels[ids[0]]
            return result,ids,rel.reltype,rel.target_ref,rel.is_external
        actual=run(docxtpl_native)
        self.assertEqual(actual,run(docxtpl));self.assertEqual(actual[1][0],actual[1][1])

    def test_real_internal_relationship(self):
        from docx.opc.packuri import PackURI
        from docx.opc.part import Part
        def run(engine):
            master=Document();child=Document()
            part=Part(PackURI('/word/custom/test1.xml'),'application/xml',b'<test/>',child.part.package)
            rid=child.part.relate_to(part,'urn:test:relationship')
            composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            try:
                result=composer.add_relationship(child.part,master.part,child.part.rels[rid])
                return 'ok',result.target_part.blob
            except Exception as exc:return type(exc).__name__,str(exc)
        self.assertEqual(run(docxtpl),('ok',b'<test/>'))
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_internal_parts_and_nested_relationships(self):
        from docx.opc.packuri import PackURI
        from docx.opc.part import Part
        def run(engine):
            master=Document();child=Document()
            occupied=Part(PackURI('/word/custom/test1.xml'),'application/xml',b'<occupied/>',master.part.package)
            master.part.relate_to(occupied,'urn:test:occupied')
            another=Part(PackURI('/word/custom/test3.xml'),'application/xml',b'<occupied3/>',master.part.package)
            master.part.relate_to(another,'urn:test:occupied3')
            parent=Part(PackURI('/word/custom/test1.xml'),'application/xml',b'<parent/>',child.part.package)
            nested=Part(PackURI('/word/custom/nested1.xml'),'application/xml',b'<nested/>',child.part.package)
            parent.relate_to(nested,'urn:test:nested')
            parent.relate_to('https://nested.invalid',RT.HYPERLINK,is_external=True)
            first=parent.rels.pop('rId1');parent.rels['rId1']=first
            rid=child.part.relate_to(parent,'urn:test:parent')
            composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            new_rel=composer.add_relationship(child.part,master.part,child.part.rels[rid])
            new_part=new_rel.target_part
            refs=[(rel.rId,rel.reltype,rel.target_ref if rel.is_external else (str(rel.target_part.partname),rel.target_part.blob)) for rel in new_part.rels.values()]
            return str(new_part.partname),new_part.blob,refs
        actual=run(docxtpl_native)
        self.assertEqual(actual,run(docxtpl));self.assertEqual(actual[0],'/word/custom/test2.xml')

    def test_invalid_nested_relationship_keeps_created_parent(self):
        from docx.opc.packuri import PackURI
        from docx.opc.part import Part
        def run(engine,invalid):
            master=Document();child=Document()
            parent=Part(PackURI('/word/custom/test1.xml'),'application/xml',b'<parent/>',child.part.package)
            nested_id=parent.relate_to('https://nested.invalid',RT.HYPERLINK,is_external=True)
            parent.rels[nested_id]._rId=invalid
            rid=child.part.relate_to(parent,'urn:test:parent')
            composer=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer(master)
            try:composer.add_relationship(child.part,master.part,child.part.rels[rid]);error=None
            except Exception as exc:error=(type(exc).__name__,exc.args)
            created=[(str(rel.target_part.partname),rel.target_part.blob) for rel in master.part.rels.values() if rel.reltype=='urn:test:parent']
            return error,created
        for invalid in ('bad','rId'):
            with self.subTest(invalid=invalid):self.assertEqual(run(docxtpl_native,invalid),run(docxtpl,invalid))

    def test_recursive_relationship_override_order(self):
        from docx.opc.packuri import PackURI
        from docx.opc.part import Part
        def run(engine):
            master=Document();child=Document();trace=[]
            parent=Part(PackURI('/word/custom/test1.xml'),'application/xml',b'<parent/>',child.part.package)
            nested=Part(PackURI('/word/custom/nested1.xml'),'application/xml',b'<nested/>',child.part.package)
            parent.relate_to(nested,'urn:test:nested');parent.relate_to('https://nested.invalid',RT.HYPERLINK,is_external=True)
            rid=child.part.relate_to(parent,'urn:test:parent')
            cls=importlib.import_module(engine.__name__+'.subdoc').SubdocComposer
            class Custom(cls):
                def add_relationship(self,src,dst,rel):
                    trace.append((rel.rId,rel.reltype,rel.is_external))
                    return super().add_relationship(src,dst,rel)
            result=Custom(master).add_relationship(child.part,master.part,child.part.rels[rid])
            return trace,str(result.target_part.partname)
        self.assertEqual(run(docxtpl_native),run(docxtpl))


if __name__=='__main__':unittest.main()
