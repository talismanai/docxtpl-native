"""Conservative status for individually curated compatibility contracts."""
from collections import Counter
from .reference_fingerprint import REFERENCE_COMMIT, EXPECTED
from .standalone_evidence import CASES as STANDALONE_CASES, valid_wheel_evidence

PINNED_REFERENCE_SOURCE = {'commit':REFERENCE_COMMIT,'expected_files':len(EXPECTED),'matched':True,'files':EXPECTED}

IMPLEMENTATIONS={'native','python_bridge','python_helper','not_implemented','unassessed'}
# Only preserved behaviour can be parity evidence. Leaving that scope needs a
# recorded decision; a divergence or exclusion never qualifies.
SCOPES={'preserved','divergence','out_of_scope'}


def assess(catalog, report, revision, expected_versions=None, expected_reference_source=PINNED_REFERENCE_SOURCE,
           standalone_reports=()):
    contracts=catalog['contracts']
    ids=[entry['id'] for entry in contracts]
    if len(ids)!=len(set(ids)):raise ValueError('duplicate contract id')
    for entry in contracts:
        if not entry.get('reference') or not entry.get('description'):
            raise ValueError('contract requires reference and description: '+entry['id'])
        if entry.get('implementation') not in IMPLEMENTATIONS:
            raise ValueError('invalid implementation label: '+entry['id'])
        if entry.get('scope') not in SCOPES:
            raise ValueError('invalid scope: '+entry['id'])
        if entry['scope']!='preserved' and not entry.get('scope_decision'):
            raise ValueError('a scope change needs a recorded decision: '+entry['id'])
        if len(entry['cases'])!=len(set(entry['cases'])):raise ValueError('duplicate case binding')
    valid=bool(report and report.get('source_revision')==revision and report.get('mode')=='differential'
        and report.get('candidate')=='docxtpl_native' and report.get('reference')=='docxtpl'
        and report.get('capabilities',{}).get('native_available') is True
        and report.get('capabilities',{}).get('reference_fallback') is False
        and report.get('visual_policy')=='required'
        and report.get('selection',{}).get('complete') is True)
    cases=report.get('cases',[]) if report else []
    ids_in_report=[case['id'] for case in cases]
    failures=sum(case['status'] not in ('pass','divergence') for case in cases)
    diverged=sum(case['status']=='divergence' for case in cases)
    expected={'total':len(cases),'passed':len(cases)-failures-diverged,'gaps_or_errors':failures}
    if diverged:expected['divergences']=diverged
    valid=valid and bool(cases) and len(set(ids_in_report))==len(ids_in_report) and report.get('counts')==expected
    valid=valid and report.get('status')==('gaps' if failures else 'pass')
    if expected_versions is not None:
        valid=valid and all(all(case.get(role,{}).get('dependencies',{}).get(package)==version
            for role in ('reference','candidate') for package,version in expected_versions.items()) for case in cases)
    if expected_reference_source is not None:
        valid=valid and all(all(case.get(role,{}).get('reference_source') == expected_reference_source
            for role in ('reference','candidate')) for case in cases)
    # Installation is not a differential test: only its dedicated, validated
    # report can supply evidence for these IDs.
    statuses={case['id']:case['status'] for case in cases if case['id'] not in STANDALONE_CASES} if valid else {}
    standalone_by_case = {}
    for evidence in standalone_reports:
        case = evidence.get('case') if isinstance(evidence, dict) else None
        if isinstance(case, str) and case in STANDALONE_CASES:
            standalone_by_case.setdefault(case, []).append(evidence)
    standalone_status = {}
    for case, evidence in standalone_by_case.items():
        accepted = len(evidence) == 1 and valid_wheel_evidence(case, evidence[0], revision, expected_versions)
        standalone_status[case] = 'pass' if accepted else 'unverified'
        if accepted:
            statuses[case] = 'pass'
    result=[]
    for entry in contracts:
        observed={case:statuses.get(case,'unverified') for case in entry['cases']}
        if any(status not in ('pass','unverified','divergence') for status in observed.values()):state='failing'
        elif not observed or any(status=='unverified' for status in observed.values()):state='unverified'
        # A registered divergence is documented, never parity.
        elif any(status=='divergence' for status in observed.values()):state='divergent'
        else:state='examples_passed'
        # A reviewed contract still requires all named cases and same-revision evidence.
        qualified=(state=='examples_passed' and entry['scope']=='preserved' and entry['reviewed'] is True
                   and not entry.get('partitions',{}).get('pending',[]))
        result.append({**entry,'case_statuses':observed,'state':state,'qualified':qualified})
    return {'evidence_valid':valid,'standalone_case_statuses':standalone_status,'revision':revision,'notice':catalog['notice'],
            'counts':dict(Counter(item['state'] for item in result)),
            'scopes':dict(Counter(item['scope'] for item in result)),
            'qualified':sum(item['qualified'] for item in result),'contracts':result}


def markdown(result):
    def cell(value):return str(value).replace('|','\\|').replace('\n',' ')
    lines=['# Behavioral specification evidence','',
        'Catalog scope is non-exhaustive. Examples are evidence, not proof of full compatibility.',
        '', 'Engine revision: `'+result['revision']+'`',
        'Eligible differential evidence: '+str(result['evidence_valid']),
        'Standalone case evidence: '+str(result['standalone_case_statuses']),
        'Qualified contracts: '+str(result['qualified']), '',
        '| Contract | Named examples | State | Execution assessment | Pending partitions |',
        '| --- | ---: | --- | --- | --- |']
    for item in result['contracts']:
        lines.append('| '+' | '.join(map(cell,[item['id'],len(item['cases']),item['state'],item['implementation'],
            ', '.join(item.get('partitions',{}).get('pending',[])) or 'manual decomposition/review']))+' |')
    return '\n'.join(lines)+'\n'
