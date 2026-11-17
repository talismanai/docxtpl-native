"""Test-design completeness, separate from execution success and native ownership."""


def assess_tests(catalog, known_cases):
    missing = []
    dangling = []
    pending = []
    undecomposed = []
    unbound_partitions = []
    out_of_scope = []
    undecided = []
    ids = [entry['id'] for entry in catalog['contracts']]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate contract id')
    for entry in catalog['contracts']:
        cases = entry['cases']
        if not cases:
            missing.append(entry['id'])
        if len(cases) != len(set(cases)):
            raise ValueError('duplicate case binding: '+entry['id'])
        for case in cases:
            if case not in known_cases:
                dangling.append({'contract': entry['id'], 'case': case})
        partitions = entry.get('partitions')
        if not partitions or not partitions.get('covered'):
            undecomposed.append(entry['id'])
        if partitions and partitions.get('pending'):
            pending.append({'contract': entry['id'], 'partitions': partitions['pending']})
        # A partition left out of the preserved scope needs its recorded decision.
        for partition in (partitions or {}).get('out_of_scope', []):
            out_of_scope.append({'contract': entry['id'], 'partition': partition})
            if not entry.get('partition_decisions', {}).get(partition):
                undecided.append({'contract': entry['id'], 'partition': partition})
        for partition in (partitions or {}).get('covered', []):
            bindings = entry.get('partition_cases', {}).get(partition, [])
            if not bindings or any(case not in cases or case not in known_cases for case in bindings):
                unbound_partitions.append({'contract': entry['id'], 'partition': partition})
    return {'scope': 'enumerated catalog only; not the entire upstream object model',
            'contracts': len(ids), 'bound_contracts': len(ids)-len(missing),
            'missing_cases': missing, 'dangling_cases': dangling,
            'pending_partitions': pending, 'missing_partition_evidence': undecomposed,
            'unbound_covered_partitions': unbound_partitions,
            'out_of_scope_partitions': out_of_scope, 'undecided_out_of_scope_partitions': undecided,
            'complete': not (missing or dangling or pending or undecomposed or unbound_partitions or undecided)}
