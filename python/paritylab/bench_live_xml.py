"""Compare the former and native live-part transforms, excluding DOCX IO."""
import json
import os
import platform
import re
import statistics
import time

import docxtpl_native


class Document:
    def patch_xml(self,source):return source


class Part:
    partname='/word/document.xml'


class Session:
    def render(self,name,source):return source


def previous(document,session,source,part):
    document.current_rendering_part=part
    source=re.sub(r'<w:p([ >])',r'\n<w:p\1',document.patch_xml(source))
    rendered=session.render(str(part.partname).lstrip('/'),source)
    rendered=re.sub(r'\n<w:p([ >])',r'<w:p\1',rendered)
    for initial,result in (('{_{','{{'),('}_}','}}'),('{_%','{%'),('%_}','%}')):
        rendered=rendered.replace(initial,result)
    return docxtpl_native._extension().resolve_listing_xml(rendered)


def main():
    native=docxtpl_native._extension().render_live_xml
    document=Document();session=Session();part=Part();iterations=3000
    inputs={'plain':'<w:p>'+('plain text '*1500)+'</w:p>',
            'markers':'<w:p>'+('{_{word}_} '*1500)+'</w:p>'}
    observations={}
    for label,source in inputs.items():
        expected=previous(document,session,source,part)
        assert native(document,session,source,part)==expected
        for _ in range(20):
            assert previous(document,session,source,part)==native(document,session,source,part)
        samples=[]
        for index in range(9):
            pair={}
            for name in (('previous','native') if index%2==0 else ('native','previous')):
                func=previous if name=='previous' else native
                start=time.perf_counter_ns()
                for _ in range(iterations):
                    assert func(document,session,source,part)==expected
                pair[name]=(time.perf_counter_ns()-start)/iterations
            samples.append(pair)
        observations[label]={'samples':samples,
            'median_previous_over_native':statistics.median(s['previous']/s['native'] for s in samples)}
    print(json.dumps({'source_revision':os.environ.get('GITHUB_SHA'),'python':platform.python_version(),
        'iterations':iterations,'warmups':20,'results':observations,
        'scope':'Live-part transformation with stub callbacks and 16 KB XML; no template compilation or DOCX IO.'},indent=2))


if __name__=='__main__':main()
