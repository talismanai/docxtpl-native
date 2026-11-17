"""Catalog freshness and executable binding checks, independent of native builds."""
import json
import re
from pathlib import Path
import unittest
from jinja2.defaults import DEFAULT_FILTERS,DEFAULT_TESTS,DEFAULT_NAMESPACE
from paritylab.protocols import PROBES

ROOT=Path(__file__).resolve().parent.parent

def decision_pointers(texts):
    """Every `docs/<file>.md` or `docs/<file>.md#<section>` named in `texts`."""
    found=set()
    for text in texts:found.update(re.findall(r'docs/[A-Za-z_]+\.md(?:#[\w-]+)?',str(text)))
    return found

def heading_slugs(path):
    """The anchors GitHub gives the headings of a Markdown file."""
    slugs,fenced=set(),False
    for line in path.read_text().splitlines():
        if line.startswith('```'):fenced=not fenced;continue
        heading=re.match(r'#{1,6}\s+(.*)',line)
        if heading and not fenced:
            slugs.add(re.sub(r'[^\w\- ]','',heading.group(1).strip().lower()).replace(' ','-'))
    return slugs

def unresolved_pointers(pointers):
    """The pointers whose file is missing or whose section is not a heading."""
    missing=[]
    for pointer in sorted(pointers):
        path,_,section=pointer.partition('#')
        if not (ROOT/path).is_file() or (section and section not in heading_slugs(ROOT/path)):missing.append(pointer)
    return missing

class SpecCatalogTests(unittest.TestCase):
    def setUp(self):self.catalog=json.loads(Path('compatibility/spec_contracts.json').read_text())
    def test_all_pinned_builtin_registry_names_are_explicit(self):
        names={c['id'] for c in self.catalog['contracts']}
        expected={'jinja.builtin.'+kind+'.'+name for kind,registry in [('filter',DEFAULT_FILTERS),('test',DEFAULT_TESTS),('global',DEFAULT_NAMESPACE)] for name in registry}
        actual={x for x in names if x.startswith('jinja.builtin.')}
        self.assertEqual(actual,expected)
    def test_every_contract_has_a_scope(self):
        scopes={c['id']:c.get('scope') for c in self.catalog['contracts']}
        self.assertEqual({scope for scope in scopes.values()}-{'preserved','divergence','out_of_scope'},set())
        for c in self.catalog['contracts']:
            if c['scope']!='preserved':self.assertTrue(c.get('scope_decision'),c['id'])
    def test_divergences_are_pinned_by_existing_tests(self):
        import importlib
        ids=[d['id'] for d in self.catalog['divergences']]
        self.assertEqual(len(ids),len(set(ids)))
        for divergence in self.catalog['divergences']:
            self.assertTrue(divergence['description'] and divergence['decision'] and divergence['tests'],divergence['id'])
            for name in divergence['tests']:
                module,cls,method=name.split('.')
                self.assertTrue(callable(getattr(getattr(importlib.import_module(module),cls),method)),name)
    def test_protocol_bindings_resolve_to_executable_cases(self):
        for c in self.catalog['contracts']:
            for case in c['cases']:
                if case.startswith('protocol/'):
                    self.assertIn(case.split('/',1)[1],PROBES,(c['id'],case))
    def test_every_enumerated_docxtpl_api_has_a_direct_or_integration_probe(self):
        entries=[c for c in self.catalog['contracts'] if c['id'].startswith('docxtpl.api.')]
        self.assertEqual(len(entries),56)
        self.assertEqual([c['id'] for c in entries if not c['cases']],[])
    def test_builtin_partitions_are_bound_to_evidence(self):
        # A partition leaves `pending` only with differential cases bound to
        # it. Whether the contract is reviewed is the owner's decision, not an
        # invariant of the catalog.
        for c in self.catalog['contracts']:
            if c['id'].startswith('jinja.builtin.'):
                self.assertTrue(c['partitions']['covered'],c['id'])
                for partition in c['partitions']['covered']:
                    cases=c.get('partition_cases',{}).get(partition)
                    self.assertTrue(cases,c['id']+': '+partition)
                    self.assertTrue(set(cases)<=set(c['cases']),c['id']+': '+partition)
    def test_recorded_decisions_point_to_documents_that_exist(self):
        # A decision is only traceable while its pointer resolves: every
        # `docs/<file>.md` named by a decision must be in the repository, and
        # a `#section` after it must be a heading of that file.
        from paritylab.divergences import DIVERGENCES
        known=json.loads(Path('compatibility/known_failures.json').read_text())
        texts=[divergence.decision for divergence in DIVERGENCES.values()]
        for contract in self.catalog['contracts']:
            texts.append(contract.get('scope_decision',''))
            texts.extend(contract.get('partition_decisions',{}).values())
        texts.extend(divergence['decision'] for divergence in self.catalog['divergences'])
        texts.extend(cause['accepted']['decision'] for cause in known['causes'].values())
        pointers=decision_pointers(texts)
        self.assertTrue(pointers)
        self.assertEqual(unresolved_pointers(pointers),[])
    def test_a_pointer_to_a_missing_section_is_unresolved(self):
        # The check above must catch a section that is not a heading, not only
        # a missing file.
        self.assertEqual(unresolved_pointers({'docs/STATUS.md#accepted-debts'}),[])
        self.assertEqual(unresolved_pointers({'docs/STATUS.md#no-such-section'}),['docs/STATUS.md#no-such-section'])
        self.assertEqual(unresolved_pointers({'docs/NO_SUCH_FILE.md'}),['docs/NO_SUCH_FILE.md'])
    def test_api_partitions_are_bound_or_decided(self):
        # docxtpl's public API is covered partition by partition; internal
        # steps keep only their error occurrence, the rest has a decision.
        for c in self.catalog['contracts']:
            if c['id'].startswith('docxtpl.api.'):
                self.assertEqual(c['partitions']['pending'],[],c['id'])
                for partition in c['partitions']['covered']:
                    self.assertTrue(set(c['partition_cases'].get(partition,[]))&set(c['cases']),c['id']+': '+partition)
                for partition in c['partitions'].get('out_of_scope',[]):
                    self.assertTrue(c.get('partition_decisions',{}).get(partition),c['id']+': '+partition)
    def test_internal_step_table_matches_the_reference(self):
        import docxtpl.subdoc,docxtpl.template
        from paritylab.internal_error_probes import STEPS,reference_step
        classes={'template':docxtpl.template.DocxTemplate,'composer':docxtpl.subdoc.SubdocComposer}
        for owner,steps in STEPS.items():
            for name,step in steps.items():
                self.assertEqual(step,reference_step(classes[owner],name),owner+'.'+name)
    def test_documented_source_methods_are_not_omitted(self):
        import ast,hashlib,docxtpl
        root=Path(docxtpl.__file__).parent
        expected={};special={'__init__','__str__','__html__','__getattr__'}
        for filename in ['template.py','richtext.py','inline_image.py','listing.py','subdoc.py']:
            data=(root/filename).read_bytes();blob=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
            for cls in ast.parse(data).body:
                if not isinstance(cls,ast.ClassDef):continue
                for method in cls.body:
                    if isinstance(method,(ast.FunctionDef,ast.AsyncFunctionDef)) and (not method.name.startswith('_') or method.name in special):
                        expected['docxtpl.api.'+cls.name+'.'+method.name]=(blob,ast.unparse(method.args))
        actual={c['id']:(c['reference_git_blob'],c['reference_signature']) for c in self.catalog['contracts'] if c['id'].startswith('docxtpl.api.')}
        self.assertEqual(actual,expected,'Pinned source changed or API contract omitted')

    def test_grammar_symbols_and_all_grammar_oracles_are_bound(self):
        import jinja2.compiler
        from paritylab.language_spec_probes import PROBES as grammar
        contracts=[c for c in self.catalog['contracts'] if c['id'].startswith('jinja.language.')]
        self.assertTrue({'protocol/'+name for name in grammar}.issubset(
            {case for c in contracts for case in c['cases']}))
        for c in contracts:
            # every partition is bound to differential evidence
            self.assertEqual(c['partitions']['pending'],[],c['id'])
            for partition in c['partitions']['covered']:
                self.assertTrue(set(c['partition_cases'].get(partition,[]))&set(c['cases']),c['id']+': '+partition)
            for symbol in c['reference_symbols']:
                self.assertTrue(hasattr(jinja2.compiler.CodeGenerator,symbol.rsplit('.',1)[1]),symbol)
