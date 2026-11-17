"""Static API inventory and conservative traceability to differential evidence."""
from __future__ import annotations

import importlib
import importlib.metadata
import inspect


PROTOCOL_MEMBERS={'__iter__','__next__','__getitem__','__setitem__','__delitem__','__len__',
                  '__bool__','__call__','__getattr__','__html__','__str__','__eq__'}
ESCAPE_HATCHES={'_element','_p','_r','_tc','_tbl','_sectPr'}
MODULES={
    'docxtpl':['docxtpl','docxtpl.template','docxtpl.richtext','docxtpl.inline_image','docxtpl.listing','docxtpl.subdoc'],
    'jinja2':['jinja2','jinja2.environment','jinja2.runtime','jinja2.loaders','jinja2.ext','jinja2.sandbox'],
}
# Scope decided on 2026-11-15 (docs/CONTRACT.md). The native engine replaces
# Jinja's compiler: its nodes, parser, lexer and code generator are not part
# of the contract (observable differences are the accepted jinja.internal.*
# divergences), and bytecode caches are refused.
EXCLUDED_OWNERS=('jinja2.nodes','jinja2.compiler','jinja2.parser','jinja2.lexer','jinja2.bccache',
                 'jinja2.visitor','jinja2.optimizer','jinja2.idtracking','jinja2.debug')
# python-docx, lxml and docxcompose objects reach users unchanged, so their API
# is theirs. The contract keeps only the classes the package hands out.
PASS_THROUGH={'python-docx':['docx.document.Document'],'lxml':['lxml.etree._Element'],
              'docxcompose':['docxcompose.composer.Composer']}


def signature(value):
    if isinstance(value,(staticmethod,classmethod)):value=value.__func__
    try:return str(inspect.signature(value))
    except (ValueError,TypeError):return None


def inventory():
    scopes={key:list(value) for key,value in MODULES.items()}
    symbols={};errors=[]
    for scope,classes in PASS_THROUGH.items():
        scopes[scope]=sorted({name.rsplit('.',1)[0] for name in classes})
        for name in classes:
            module,_,attribute=name.rpartition('.')
            try:value=getattr(importlib.import_module(module),attribute)
            except Exception as error:
                errors.append({'module':module,'type':type(error).__name__,'message':str(error)});continue
            symbols[name]={'id':name,'scope':scope,'kind':'class','signature':signature(value)}
    for scope,modules in scopes.items():
        prefix='docx.' if scope=='python-docx' else scope+'.'
        if scope in PASS_THROUGH:continue
        for name in modules:
            try:module=importlib.import_module(name)
            except Exception as error:
                errors.append({'module':name,'type':type(error).__name__,'message':str(error)});continue
            for exported in dir(module):
                value=inspect.getattr_static(module,exported)
                # lxml's ElementMaker dynamically turns arbitrary attribute
                # reads into XML factories; inventory must not execute them.
                if not (inspect.isclass(value) or inspect.isroutine(value)):continue
                owner=getattr(value,'__module__','')
                if not isinstance(owner,str):owner=''
                if not (owner==prefix[:-1] or owner.startswith(prefix)):continue
                if owner.startswith(EXCLUDED_OWNERS):continue
                if inspect.isclass(value):
                    identifier=owner+'.'+value.__qualname__
                    symbols.setdefault(identifier,{'id':identifier,'scope':scope,'kind':'class','signature':signature(value)})
                    for member in dir(value):
                        if member.startswith('_') and member not in PROTOCOL_MEMBERS|ESCAPE_HATCHES:continue
                        try:attribute=inspect.getattr_static(value,member)
                        except AttributeError:continue
                        key=identifier+'.'+member
                        kind='property' if isinstance(attribute,property) or inspect.isdatadescriptor(attribute) else 'method' if callable(attribute) or isinstance(attribute,(classmethod,staticmethod)) else 'attribute'
                        symbols.setdefault(key,{'id':key,'scope':scope,'kind':kind,
                            'signature':signature(attribute) if kind=='method' else None,
                            'writable':bool(attribute.fset) if isinstance(attribute,property) else None})
                elif not exported.startswith('_') and inspect.isroutine(value):
                    identifier=owner+'.'+getattr(value,'__qualname__',exported)
                    symbols.setdefault(identifier,{'id':identifier,'scope':scope,'kind':'function','signature':signature(value)})
    import jinja2.defaults
    for kind,mapping in [('filter',jinja2.defaults.DEFAULT_FILTERS),('test',jinja2.defaults.DEFAULT_TESTS),('global',jinja2.defaults.DEFAULT_NAMESPACE)]:
        for name,value in mapping.items():
            key='jinja2.'+kind+'.'+name
            symbols[key]={'id':key,'scope':'jinja2','kind':kind,'signature':signature(value)}
    versions={}
    for name in ['docxtpl','Jinja2','python-docx','lxml','docxcompose']:
        try:versions[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:versions[name]='source tree / distribution metadata unavailable'
    return {'schema_version':2,'notice':'Reflected inventory, not tested or supported API coverage; instance attributes and behavioral contracts also require review.',
            'versions':versions,'modules':scopes,'errors':errors,'symbols':sorted(symbols.values(),key=lambda item:item['id'])}


def matrix(contract,api,report=None,required_revision=None,deployment_reports=()):
    from .protocols import PROBES
    from .deployment_evidence import PREFIX as DEPLOYMENT,declared_targets,deployment_statuses
    issues=[]
    if api['errors']:issues.append('API inventory has import errors')
    for package,version in contract['versions'].items():
        if api['versions'].get(package)!=version:issues.append('reference version mismatch: '+package)
    evidence_valid=bool(report and report.get('mode')=='differential' and report.get('candidate')=='docxtpl_native'
                        and report.get('reference')=='docxtpl'
                        and report.get('capabilities',{}).get('native_available') is True
                        and report.get('capabilities',{}).get('reference_fallback') is False
                        and report.get('visual_policy')==contract['qualification']['visual']
                        and report.get('status') in ('pass','gaps'))
    if evidence_valid and '.'.join(str(report.get('python')).split('.')[:2]) not in contract['python_targets']:
        # The contract promises the declared interpreters; a run on another
        # one measures something the contract does not claim.
        evidence_valid=False
        issues.append('differential evidence ran on an undeclared Python: '+str(report.get('python')))
    if evidence_valid and report.get('selection',{}).get('complete') is not True:
        # A selection that happens to cover the named tests is not the corpus.
        evidence_valid=False
        issues.append('differential evidence is a partial run, not a complete corpus')
    if required_revision is not None and (not report or report.get('source_revision')!=required_revision):
        evidence_valid=False
        issues.append('evidence was measured on a dirty working tree, so it names no revision'
                      if report and report.get('source_tree')=='dirty'
                      else 'evidence source revision does not match checkout')
    if evidence_valid:
        # Recount the report from its own cases: a report whose summary does
        # not follow from them, or that lists a case twice, is not evidence.
        # A registered divergence is its own outcome, neither pass nor gap
        # (it still never qualifies a requirement: only 'pass' does).
        cases=report.get('cases',[]);identities=[case['id'] for case in cases]
        failures=sum(case['status'] not in ('pass','divergence') for case in cases)
        diverged=sum(case['status']=='divergence' for case in cases)
        counts={'total':len(cases),'passed':len(cases)-failures-diverged,'gaps_or_errors':failures}
        if diverged:counts['divergences']=diverged
        if not cases or len(set(identities))!=len(identities) or report.get('counts')!=counts \
                or report['status']!=('gaps' if failures else 'pass'):
            evidence_valid=False;issues.append('differential evidence has inconsistent/duplicate case accounting')
    if not evidence_valid:issues.append('required native differential evidence is missing, incomplete or ineligible')
    tested={case['id']:case['status'] for case in report.get('cases',[])
            if not case['id'].startswith(DEPLOYMENT)} if evidence_valid else {}
    # Installing on a platform is not a differential case: only its own
    # validated report, measured at this revision, proves a target.
    targets=declared_targets(contract)
    deployment,deployment_issues=deployment_statuses(contract,deployment_reports,required_revision,contract['versions'])
    issues.extend(deployment_issues)
    tested.update({name:status for name,status in deployment.items() if status=='pass'})
    identifiers={symbol['id'] for symbol in api['symbols']};bindings={};requirements=[]
    for item in contract['requirements']:
        unknown=[symbol for symbol in item['symbols'] if symbol not in identifiers]
        if unknown:issues.append('unknown API binding for '+item['id']+': '+', '.join(unknown))
        for test in item['tests']:
            if test.startswith('protocol/') and test.split('/',1)[1] not in PROBES:
                issues.append('unknown protocol binding: '+test)
            if test.startswith(DEPLOYMENT) and test not in targets:
                issues.append('unknown deployment binding: '+test)
        for symbol in item['symbols']:bindings.setdefault(symbol,[]).append(item['id'])
        statuses={test:tested.get(test,'unverified') for test in item['tests']}
        passes=bool(statuses) and all(status=='pass' for status in statuses.values())
        manual=item.get('evidence')=='manual'
        if manual and item['tests']:issues.append('a manual-evidence requirement names tests: '+item['id'])
        requirements.append({**item,'test_statuses':statuses,'named_tests_passed':passes,
                             'qualified':passes and item['reviewed'] and not unknown,'manual_evidence':manual})
    entries=[{**symbol,'requirements':bindings.get(symbol['id'],[]),
              'status':'bound_needs_review' if symbol['id'] in bindings else 'unmapped'} for symbol in api['symbols']]
    counts={'inventory_symbols':len(entries),'bound_symbols':sum(entry['status']!='unmapped' for entry in entries),
            'unmapped_symbols':sum(entry['status']=='unmapped' for entry in entries),
            'requirements':len(requirements),'unreviewed_requirements':sum(not item['reviewed'] for item in requirements),
            'qualified_requirements':sum(item['qualified'] for item in requirements),
            'manual_requirements':sum(item['manual_evidence'] for item in requirements),
            'deployment_targets':len(deployment),'verified_deployment_targets':sum(status=='pass' for status in deployment.values())}
    # A registered divergence is the contract, not a gap: the laboratory only
    # labels a case so after checking it differs exactly as declared, and the
    # declaration lives in `divergences.DIVERGENCES` with the tests that pin
    # it. A case that claims to diverge without being registered blocks.
    from .divergences import DIVERGENCES
    undeclared=sorted(name for name,status in tested.items() if status=='divergence' and name not in DIVERGENCES)
    for name in undeclared:issues.append('unregistered divergence in the evidence: '+name)
    all_cases_accounted=bool(tested) and all(status in ('pass','divergence') for status in tested.values())
    # A requirement whose evidence is manual (a person's check no automated run
    # produces) stays unqualified until that evidence exists; everything else
    # must be qualified by the machine-checked evidence alone.
    automated=not issues and all_cases_accounted and not counts['unmapped_symbols'] and all(
        item['qualified'] for item in requirements if not item['manual_evidence'])
    compatible=automated and all(item['qualified'] for item in requirements)
    return {'schema_version':1,'notice':contract['notice'],'reference_commit':contract['reference_commit'],
            'reference_versions':api['versions'],'tested_revision':report.get('source_revision') if report else None,
            'deployment_target_statuses':deployment,
            'fully_compatible':compatible,'automated_evidence_complete':automated,
            'status':'qualified' if compatible else 'blocked' if issues else 'manual_evidence_pending' if automated else 'gaps',
            'issues':issues,'counts':counts,'requirements':requirements,'api':entries,'inventory_errors':api['errors']}
