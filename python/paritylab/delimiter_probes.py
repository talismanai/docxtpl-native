"""Sequential restoration of escaped delimiters, including overlaps."""
from .protocol_base import Probe, Result, generate, observed


def direct(engine):
    template=engine.DocxTemplate(None)
    values=['{_{_%','{_%_}','%_}_}']
    results=[template.render_xml_part('<w:p>{{ value }}</w:p>',object(),{'value':value}) for value in values]
    return Result({'results':results},{})


def document(engine):
    return observed(generate(engine,'{{ value }}',{'value':'{_{_%'}))


def live(engine):
    return observed(generate(engine,'{{ echo(value) }}',{'value':'{_%_}','echo':lambda value:value}))


PROBES={
    'escaped_delimiters_overlap_direct':Probe(direct,{'results':['<w:p>{{%</w:p>','<w:p>{%}</w:p>','<w:p>%}}</w:p>']},True),
    'escaped_delimiters_overlap_document':Probe(document,{'text':'{{%'}),
    'escaped_delimiters_overlap_live':Probe(live,{'text':'{%}'}),
}
