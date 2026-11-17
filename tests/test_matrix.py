import unittest
from pathlib import Path
from paritylab.inventory import inventory,matrix


class MatrixTests(unittest.TestCase):
    def test_inventory_handles_real_c_extension_factories(self):
        result=inventory()
        self.assertFalse(result['errors'])
        names={symbol['id'] for symbol in result['symbols']}
        for name in ['docxtpl.template.DocxTemplate.render','lxml.etree._Element','docx.document.Document']:
            self.assertTrue(name in names,'missing inventory symbol '+name)

    def inputs(self):
        contract={'notice':'test contract','reference_commit':'fixed','versions':{'docxtpl':'0.20.2'},
                  'qualification':{'visual':'required'},'requirements':[{
                      'id':'identity','symbols':['render'],'tests':['protocol/callback_identity'],'reviewed':True}]}
        contract['python_targets']=['3.11','3.12']
        api={'errors':[],'versions':{'docxtpl':'0.20.2'},'symbols':[{'id':'render','scope':'docxtpl'}]}
        report={'mode':'differential','candidate':'docxtpl_native','reference':'docxtpl',
                'capabilities':{'native_available':True,'reference_fallback':False},'visual_policy':'required',
                'status':'pass','source_revision':'current','counts':{'total':1,'passed':1,'gaps_or_errors':0},
                'selection':{'suite':'all','cases':[],'protocol_cases':[],'real_manifest':False,'complete':True},
                'python':'3.12.14','cases':[{'id':'protocol/callback_identity','status':'pass'}]}
        return contract,api,report

    def test_evidence_from_an_undeclared_interpreter_qualifies_nothing(self):
        contract,api,report=self.inputs()
        report['python']='3.13.0'
        result=matrix(contract,api,report,required_revision='current')
        self.assertIn('differential evidence ran on an undeclared Python: 3.13.0',result['issues'])
        self.assertFalse(result['fully_compatible'])

    def test_a_selected_subset_is_not_a_qualifying_corpus(self):
        # A run that names exactly the tests the contract names would qualify
        # everything while most of the corpus never ran.
        contract,api,report=self.inputs()
        report['selection']={'suite':'protocols','cases':[],'protocol_cases':['callback_identity'],
                             'real_manifest':False,'complete':False}
        result=matrix(contract,api,report,required_revision='current')
        self.assertIn('differential evidence is a partial run, not a complete corpus',result['issues'])
        self.assertFalse(result['fully_compatible'])

    def test_a_report_that_does_not_say_what_it_measured_qualifies_nothing(self):
        contract,api,report=self.inputs()
        del report['selection']
        result=matrix(contract,api,report,required_revision='current')
        self.assertIn('differential evidence is a partial run, not a complete corpus',result['issues'])
        self.assertFalse(result['fully_compatible'])

    def test_closed_reviewed_toy_contract_can_qualify(self):
        self.assertTrue(matrix(*self.inputs(),required_revision='current')['fully_compatible'])

    def test_manual_evidence_is_pending_and_never_qualified(self):
        contract,api,report=self.inputs()
        contract['requirements'].append({'id':'word','symbols':[],'tests':[],'reviewed':True,'evidence':'manual'})
        result=matrix(contract,api,report,required_revision='current')
        self.assertTrue(result['automated_evidence_complete'])
        self.assertFalse(result['fully_compatible'])
        self.assertEqual(result['status'],'manual_evidence_pending')
        self.assertFalse(result['requirements'][1]['qualified'])

    def test_a_manual_requirement_names_no_test(self):
        contract,api,report=self.inputs()
        contract['requirements'][0]['evidence']='manual'
        result=matrix(contract,api,report,required_revision='current')
        self.assertFalse(result['automated_evidence_complete'])
        self.assertIn('a manual-evidence requirement names tests: identity',result['issues'])

    def test_dirty_tree_evidence_names_no_revision(self):
        # A report measured with uncommitted changes must not qualify the
        # commit it was started from: the code that ran is not that commit.
        contract,api,report=self.inputs()
        report.update(source_revision=None,source_tree='dirty')
        result=matrix(contract,api,report,required_revision='current')
        self.assertFalse(result['fully_compatible'])
        self.assertIn('evidence was measured on a dirty working tree, so it names no revision',result['issues'])

    def test_clean_checkout_evidence_qualifies_outside_ci(self):
        contract,api,report=self.inputs()
        report.update(source_revision='current',source_tree='clean_checkout')
        self.assertTrue(matrix(contract,api,report,required_revision='current')['fully_compatible'])

    def diverging(self):
        """A report whose single extra case is a registered divergence."""
        contract,api,report=self.inputs()
        report['cases'].append({'id':'protocol/unescaped_body_recovery','status':'divergence'})
        report['counts']={'total':2,'passed':1,'gaps_or_errors':0,'divergences':1}
        return contract,api,report

    def test_registered_divergence_is_neither_pass_nor_gap(self):
        contract,api,report=self.diverging()
        result=matrix(contract,api,report,required_revision='current')
        self.assertNotIn('differential evidence has inconsistent/duplicate case accounting',result['issues'])
        self.assertTrue(result['fully_compatible'])

    def test_divergence_does_not_qualify_the_requirement_it_covers(self):
        contract,api,report=self.diverging()
        contract['requirements'][0]['tests']=['protocol/unescaped_body_recovery']
        result=matrix(contract,api,report,required_revision='current')
        self.assertFalse(result['requirements'][0]['named_tests_passed'])
        self.assertFalse(result['fully_compatible'])

    def test_unregistered_divergence_blocks(self):
        contract,api,report=self.diverging()
        report['cases'][1]['id']='protocol/callback_error'
        result=matrix(contract,api,report,required_revision='current')
        self.assertIn('unregistered divergence in the evidence: protocol/callback_error',result['issues'])
        self.assertFalse(result['fully_compatible'])

    def test_a_real_gap_still_blocks(self):
        contract,api,report=self.diverging()
        report['cases'][1]['status']='gap'
        report['counts']={'total':2,'passed':1,'gaps_or_errors':1}
        report['status']='gaps'
        self.assertFalse(matrix(contract,api,report,required_revision='current')['fully_compatible'])

    def test_summary_that_does_not_follow_from_the_cases_is_rejected(self):
        contract,api,report=self.diverging()
        report['counts']['passed']=2
        result=matrix(contract,api,report,required_revision='current')
        self.assertIn('differential evidence has inconsistent/duplicate case accounting',result['issues'])

    def test_no_requirement_is_proved_by_a_registered_divergence(self):
        # A divergence never qualifies a requirement, so a requirement that
        # names one as proof can never qualify: the catalog's `divergences`
        # and its own tests are where that decision is evidenced.
        import json
        from paritylab.divergences import DIVERGENCES
        root=Path(__file__).resolve().parents[1]
        contract=json.loads((root/'compatibility'/'contract.json').read_text())
        declared={'protocol/'+name.split('/',1)[1] for name in DIVERGENCES}
        for item in contract['requirements']:
            overlap=sorted(set(item['tests'])&declared)
            self.assertEqual(overlap,[],item['id']+' is proved by a registered divergence: '+str(overlap))

    def test_passed_examples_do_not_cover_unmapped_api(self):
        contract,api,report=self.inputs();api['symbols'].append({'id':'untested_method','scope':'docxtpl'})
        result=matrix(contract,api,report)
        self.assertFalse(result['fully_compatible']);self.assertEqual(result['counts']['unmapped_symbols'],1)

    def test_self_check_is_not_native_evidence(self):
        contract,api,report=self.inputs();report['mode']='self_check';report['candidate']='docxtpl'
        result=matrix(contract,api,report)
        self.assertFalse(result['fully_compatible']);self.assertFalse(result['requirements'][0]['named_tests_passed'])

    def test_stale_commit_cannot_qualify_current_checkout(self):
        self.assertFalse(matrix(*self.inputs(),required_revision='different')['fully_compatible'])

    def test_test_binding_alone_is_not_semantic_review(self):
        contract,api,report=self.inputs();contract['requirements'][0]['reviewed']=False
        result=matrix(contract,api,report)
        self.assertTrue(result['requirements'][0]['named_tests_passed']);self.assertFalse(result['fully_compatible'])

    def test_reference_import_failure_blocks_certification(self):
        contract,api,report=self.inputs();api['errors']=[{'module':'missing'}]
        self.assertFalse(matrix(contract,api,report)['fully_compatible'])

    def test_failing_unbound_scenario_blocks_certification(self):
        contract,api,report=self.inputs();report['cases'].append({'id':'other','status':'gap'});report['status']='gaps'
        report['counts']={'total':2,'passed':1,'gaps_or_errors':1}
        self.assertFalse(matrix(contract,api,report)['fully_compatible'])

    def test_wrong_reference_version_blocks_certification(self):
        contract,api,report=self.inputs();api['versions']['docxtpl']='different'
        self.assertFalse(matrix(contract,api,report)['fully_compatible'])

    def test_duplicate_pass_cannot_overwrite_a_failure(self):
        contract,api,report=self.inputs()
        report['cases'].insert(0,{'id':'protocol/callback_identity','status':'gap'})
        report['counts']={'total':2,'passed':1,'gaps_or_errors':1};report['status']='gaps'
        self.assertFalse(matrix(contract,api,report)['fully_compatible'])

    def test_inconsistent_report_totals_are_ineligible(self):
        contract,api,report=self.inputs();report['counts']['passed']=100
        self.assertFalse(matrix(contract,api,report)['fully_compatible'])


class DeploymentEvidenceTests(unittest.TestCase):
    """A declared platform target qualifies only from its own validated report."""

    def setUp(self):
        from paritylab.standalone_evidence import CHECKER_SHA256,REFERENCE,REFERENCE_HELPER_SHA256
        self.versions={'docxtpl':'0.20.2','Jinja2':'3.1.6','python-docx':'1.2.0',
                       'lxml':'6.1.1','docxcompose':'1.4.0'}
        self.contract={'notice':'test contract','reference_commit':'fixed','versions':self.versions.copy(),
                       'qualification':{'visual':'required'},'python_targets':['3.11','3.12'],
                       'deployment_targets':[{'platform':'linux','machine':'x86_64','python':'3.11'},
                                             {'platform':'linux','machine':'aarch64','python':'3.12'}],
                       'requirements':[{'id':'deployment','symbols':[],'reviewed':True,
                                        'tests':['deployment/linux_x86_64_cp311','deployment/linux_aarch64_cp312']}]}
        self.api={'errors':[],'versions':self.versions.copy(),'symbols':[]}
        self.report={'mode':'differential','candidate':'docxtpl_native','reference':'docxtpl',
                     'capabilities':{'native_available':True,'reference_fallback':False},'visual_policy':'required',
                     'status':'pass','source_revision':'current','counts':{'total':1,'passed':1,'gaps_or_errors':0},
                     'selection':{'suite':'all','cases':[],'protocol_cases':[],'real_manifest':False,'complete':True},
                     'python':'3.12.14','cases':[{'id':'protocol/callback_identity','status':'pass'}]}
        self.reference=REFERENCE;self.helper=REFERENCE_HELPER_SHA256;self.checker=CHECKER_SHA256

    def evidence(self,machine='x86_64',python='3.11.17',revision='current'):
        import copy
        short=''.join(python.split('.')[:2])
        root='/tmp/docxtpl-wheel-smoke-fixture/venv'
        return {'case':'standalone/wheel_python'+short,'status':'pass','mode':'local_wheel_install',
                'source_revision':revision,'python':python,'platform':'linux','machine':machine,
                'environment':root,'module':root+'/lib/site-packages/docxtpl_native/__init__.py',
                'extension':root+'/lib/site-packages/docxtpl_native/_native.cpython-'+short+'.so',
                'laboratory':root+'/lib/site-packages/paritylab/reference_fingerprint.py',
                'wheel':'docxtpl_native-0.1.0-cp'+short+'-cp'+short+'-manylinux_2_28_'+machine+'.whl',
                'wheel_sha256':'a'*64,'extension_sha256':'b'*64,
                'fingerprint_helper_sha256':self.helper,'checker_sha256':self.checker,
                'dependency_site':'/tmp/dependencies/site-packages',
                'reference_module':'/tmp/dependencies/site-packages/docxtpl/__init__.py',
                'reference_source':copy.deepcopy(self.reference),'dependencies':self.versions.copy(),
                'native_available':True,'reference_fallback':False,'rendered_text':'Café Ada'}

    def run_matrix(self,reports):
        return matrix(self.contract,self.api,self.report,required_revision='current',deployment_reports=reports)

    def test_one_report_per_declared_target_qualifies_the_requirement(self):
        result=self.run_matrix([self.evidence(),self.evidence('aarch64','3.12.14')])
        self.assertEqual(result['issues'],[])
        self.assertEqual(result['counts']['verified_deployment_targets'],2)
        self.assertTrue(result['requirements'][0]['qualified'])

    def test_a_missing_target_leaves_the_requirement_unqualified(self):
        result=self.run_matrix([self.evidence()])
        self.assertEqual(result['deployment_target_statuses']['deployment/linux_aarch64_cp312'],'unverified')
        self.assertFalse(result['requirements'][0]['qualified'])
        # A workstation has no containers: absent evidence is not an issue.
        self.assertEqual(result['issues'],[])

    def test_no_report_at_all_keeps_the_matrix_free_of_issues(self):
        result=self.run_matrix([])
        self.assertEqual(result['issues'],[])
        self.assertEqual(result['counts']['verified_deployment_targets'],0)
        self.assertFalse(result['requirements'][0]['qualified'])

    def test_evidence_from_another_revision_blocks(self):
        result=self.run_matrix([self.evidence(revision='older'),self.evidence('aarch64','3.12.14')])
        self.assertIn('deployment report 1 is not valid same-revision wheel evidence',result['issues'])
        self.assertFalse(result['requirements'][0]['qualified'])

    def test_mutated_evidence_never_verifies_a_target(self):
        for field,value in [('native_available',False),('reference_fallback',True),('status','error'),
                            ('platform','darwin'),('rendered_text','Café Grace'),('checker_sha256','c'*64),
                            ('mode','source_tree'),('wheel','docxtpl_native-0.1.0-py3-none-any.whl')]:
            with self.subTest(field=field):
                mutated=self.evidence();mutated[field]=value
                result=self.run_matrix([mutated,self.evidence('aarch64','3.12.14')])
                self.assertEqual(result['deployment_target_statuses']['deployment/linux_x86_64_cp311'],'unverified')
                self.assertFalse(result['requirements'][0]['qualified'])
                self.assertTrue(result['issues'])

    def test_a_target_outside_the_contract_is_not_evidence(self):
        result=self.run_matrix([self.evidence(),self.evidence('aarch64','3.12.14'),self.evidence('aarch64','3.11.17')])
        self.assertIn('deployment report names an undeclared target: deployment/linux_aarch64_cp311',result['issues'])
        self.assertFalse(result['fully_compatible'])

    def test_two_reports_for_one_target_cancel_each_other(self):
        result=self.run_matrix([self.evidence(),self.evidence(),self.evidence('aarch64','3.12.14')])
        self.assertIn('more than one deployment report for deployment/linux_x86_64_cp311',result['issues'])
        self.assertEqual(result['deployment_target_statuses']['deployment/linux_x86_64_cp311'],'unverified')
        self.assertFalse(result['requirements'][0]['qualified'])

    def test_unreadable_evidence_is_an_issue_not_a_pass(self):
        result=self.run_matrix([None,'not a report',{'case':'standalone/wheel_python311'}])
        self.assertEqual(len([issue for issue in result['issues'] if 'not valid same-revision' in issue]),3)
        self.assertFalse(result['requirements'][0]['qualified'])

    def test_the_differential_corpus_cannot_supply_a_deployment_id(self):
        # A corpus case named like a target must not stand in for installing
        # on it: these ids come only from the dedicated report.
        self.report['cases'].append({'id':'deployment/linux_x86_64_cp311','status':'pass'})
        self.report['counts']={'total':2,'passed':2,'gaps_or_errors':0}
        result=self.run_matrix([self.evidence('aarch64','3.12.14')])
        self.assertEqual(result['deployment_target_statuses']['deployment/linux_x86_64_cp311'],'unverified')
        self.assertFalse(result['requirements'][0]['qualified'])

    def test_a_requirement_cannot_name_an_undeclared_target(self):
        self.contract['requirements'][0]['tests'].append('deployment/linux_riscv64_cp313')
        result=self.run_matrix([self.evidence(),self.evidence('aarch64','3.12.14')])
        self.assertIn('unknown deployment binding: deployment/linux_riscv64_cp313',result['issues'])
        self.assertFalse(result['fully_compatible'])

    def test_the_real_contract_declares_every_target_it_names(self):
        import json
        from pathlib import Path as FilePath
        from paritylab.deployment_evidence import PREFIX,declared_targets
        contract=json.loads(FilePath('compatibility/contract.json').read_text())
        declared=declared_targets(contract)
        named=[test for item in contract['requirements'] for test in item['tests'] if test.startswith(PREFIX)]
        self.assertTrue(named)
        self.assertEqual(sorted(set(named)),sorted(declared))
