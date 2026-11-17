"""Reproduce the classifier benchmark against the pre-port Python baseline."""
import json, os, platform, statistics, timeit
from docxtpl_native.simple_frontend import context_names

def baseline(context):
    if type(context) is not dict:return None
    pending=[context];visited=set()
    while pending:
        value=pending.pop();kind=type(value)
        if kind in (str,int,float,bool,bytes,type(None)):continue
        if id(value) in visited:continue
        visited.add(id(value))
        if kind is dict:
            if any(type(key) is not str for key in value):return None
            pending.extend(value.values())
        elif kind in (list,tuple):pending.extend(value)
        else:return None
    return frozenset(context)

shared={'a':[1,2,3],'b':('x',None,True)}
contexts={'small':{'name':'Ada','age':36}, 'nested':{'a':shared,'b':shared,'items':[{'name':str(i),'v':i} for i in range(50)]}}
result={'revision':os.environ.get('GITHUB_SHA', 'unknown'),'python':platform.python_version(),'samples':9,'iterations':2000,'cases':{}}
for name,context in contexts.items():
    assert baseline(context)==context_names(context)
    paired=[]
    for i in range(9):
        functions=(baseline,context_names) if i%2==0 else (context_names,baseline)
        times={fn.__name__:timeit.timeit(lambda:fn(context),number=2000) for fn in functions}
        paired.append({'python_s':times['baseline'],'rust_s':times['context_names']})
    result['cases'][name]={'samples':paired,'median_ratio_python_over_rust':statistics.median(x['python_s']/x['rust_s'] for x in paired)}
print(json.dumps(result,indent=2))
