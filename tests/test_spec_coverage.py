import unittest
from paritylab.spec_coverage import assess, PINNED_REFERENCE_SOURCE

class SpecCoverageTests(unittest.TestCase):
    def setUp(self):
        self.catalog={'notice':'non-exhaustive','contracts':[{'id':'a','description':'test','reference':'source.py',
                      'cases':['protocol/a'],'implementation':'native','scope':'preserved','reviewed':False}]}
        self.report={'source_revision':'abc','mode':'differential','candidate':'docxtpl_native','reference':'docxtpl',
                     'capabilities':{'native_available':True,'reference_fallback':False},'visual_policy':'required',
                     'status':'pass','counts':{'total':1,'passed':1,'gaps_or_errors':0},
                     'selection':{'suite':'all','cases':[],'protocol_cases':[],'real_manifest':False,'complete':True},
                     'cases':[{'id':'protocol/a','status':'pass',
                               'reference':{'reference_source':PINNED_REFERENCE_SOURCE},
                               'candidate':{'reference_source':PINNED_REFERENCE_SOURCE}}]}
    def test_pass_does_not_certify_unreviewed_contract(self):
        result=assess(self.catalog,self.report,'abc')
        self.assertEqual(result['contracts'][0]['state'],'examples_passed');self.assertEqual(result['qualified'],0)
    def test_wrong_revision_cannot_use_stale_evidence(self):
        result=assess(self.catalog,self.report,'different')
        self.assertFalse(result['evidence_valid']);self.assertEqual(result['contracts'][0]['state'],'unverified')
    def test_failure_is_not_a_pass(self):
        self.report.update(status='gaps',counts={'total':1,'passed':0,'gaps_or_errors':1},cases=[{'id':'protocol/a','status':'gap','reference':{'reference_source':PINNED_REFERENCE_SOURCE},
                  'candidate':{'reference_source':PINNED_REFERENCE_SOURCE}}])
        self.assertEqual(assess(self.catalog,self.report,'abc')['contracts'][0]['state'],'failing')
    def test_duplicate_case_results_are_ineligible(self):
        self.report['cases'].append({'id':'protocol/a','status':'pass'})
        self.report['counts']={'total':2,'passed':2,'gaps_or_errors':0}
        self.assertFalse(assess(self.catalog,self.report,'abc')['evidence_valid'])
    def test_missing_case_never_passes(self):
        self.catalog['contracts'][0]['cases'].append('protocol/missing')
        self.assertEqual(assess(self.catalog,self.report,'abc')['contracts'][0]['state'],'unverified')
    def test_duplicate_contract_is_rejected(self):
        self.catalog['contracts'].append(self.catalog['contracts'][0].copy())
        with self.assertRaises(ValueError):assess(self.catalog,self.report,'abc')

    def test_reviewed_preserved_contract_qualifies(self):
        self.catalog['contracts'][0].update(reviewed=True,partitions={'pending':[]})
        self.assertEqual(assess(self.catalog,self.report,'abc')['qualified'],1)

    def test_scope_is_required_and_closed(self):
        for scope in (None,'pending','Preserved'):
            with self.subTest(scope=scope):
                self.catalog['contracts'][0]['scope']=scope
                with self.assertRaisesRegex(ValueError,'invalid scope'):assess(self.catalog,self.report,'abc')
        del self.catalog['contracts'][0]['scope']
        with self.assertRaisesRegex(ValueError,'invalid scope'):assess(self.catalog,self.report,'abc')

    def test_scope_change_needs_a_decision(self):
        self.catalog['contracts'][0]['scope']='divergence'
        with self.assertRaisesRegex(ValueError,'recorded decision'):assess(self.catalog,self.report,'abc')

    def test_divergence_never_qualifies_as_parity(self):
        for scope in ('divergence','out_of_scope'):
            with self.subTest(scope=scope):
                self.catalog['contracts'][0].update(scope=scope,scope_decision='docs/API.md',reviewed=True,partitions={'pending':[]})
                result=assess(self.catalog,self.report,'abc')
                self.assertEqual(result['contracts'][0]['state'],'examples_passed')
                self.assertEqual(result['qualified'],0)
                self.assertEqual(result['scopes'],{scope:1})

    def test_pending_partitions_block_qualification_even_after_review(self):
        self.catalog['contracts'][0].update(reviewed=True,partitions={'pending':['errors']})
        self.assertEqual(assess(self.catalog,self.report,'abc')['qualified'],0)

    def test_wrong_dependency_versions_block_evidence(self):
        self.assertFalse(assess(self.catalog,self.report,'abc',expected_versions={'Jinja2':'3.1.6'})['evidence_valid'])
        self.report['cases'][0]['reference']['dependencies']={'Jinja2':'3.1.6'}
        self.assertFalse(assess(self.catalog,self.report,'abc',expected_versions={'Jinja2':'3.1.6'})['evidence_valid'])
        self.report['cases'][0]['candidate']['dependencies']={'Jinja2':'3.1.5'}
        self.assertFalse(assess(self.catalog,self.report,'abc',expected_versions={'Jinja2':'3.1.6'})['evidence_valid'])
        self.report['cases'][0]['candidate']['dependencies']={'Jinja2':'3.1.6'}
        self.assertTrue(assess(self.catalog,self.report,'abc',expected_versions={'Jinja2':'3.1.6'})['evidence_valid'])

    def test_missing_or_mutated_reference_source_blocks_evidence(self):
        source={'commit':'fixed','expected_files':1,'matched':True,'files':{'a.py':'expected'}}
        check=lambda:assess(self.catalog,self.report,'abc',expected_reference_source=source)['evidence_valid']
        self.report['cases'][0].pop('reference')
        self.report['cases'][0].pop('candidate')
        self.assertFalse(check())
        self.report['cases'][0]['reference']={'reference_source':source.copy()}
        self.assertFalse(check())
        self.report['cases'][0]['candidate']={'reference_source':source.copy()}
        self.assertTrue(check())
        self.report['cases'][0]['candidate']['reference_source']={**source,'files':{'a.py':'mutated'}}
        self.assertFalse(check())

    def test_a_partial_run_is_not_eligible_evidence(self):
        # The catalog qualifies on the whole corpus, not on a chosen subset.
        self.report['selection']={'suite':'protocols','cases':[],'protocol_cases':['a'],
                                  'real_manifest':False,'complete':False}
        result=assess(self.catalog,self.report,'abc')
        self.assertFalse(result['evidence_valid'])
        self.assertEqual(result['counts'].get('examples_passed'),None)
