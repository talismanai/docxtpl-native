"""Structural Word tags: independent paragraph/run/table observations."""
import io,zipfile
from docx import Document
from lxml import etree
from .protocol_base import Probe,Result

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'

def render(engine,source,context):
    buffer=io.BytesIO();source.save(buffer)
    doc=engine.DocxTemplate(io.BytesIO(buffer.getvalue()));doc.render(context)
    output=io.BytesIO();doc.save(output);return output.getvalue()

def paragraph_control(show):
    def execute(engine):
        source=Document()
        for text in ['{%p if show %}','inside','{%p endif %}','after']:source.add_paragraph(text)
        blob=render(engine,source,{'show':show});parsed=Document(io.BytesIO(blob))
        return Result({'paragraphs':[p.text for p in parsed.paragraphs]},{'document.docx':blob})
    return execute

def run_control(show):
    def execute(engine):
        source=Document();p=source.add_paragraph();p.add_run('{%r if show %}')
        p.add_run('inside').bold=True;p.add_run('{%r endif %}');p.add_run('after')
        blob=render(engine,source,{'show':show});p=Document(io.BytesIO(blob)).paragraphs[0]
        return Result({'text':p.text,'bold':[r.text for r in p.runs if r.bold]},{'document.docx':blob})
    return execute

def table_tag(kind):
    def execute(engine):
        source=Document()
        if kind=='vm':
            table=source.add_table(rows=3,cols=2)
            table.cell(0,0).text='{%tr for item in items %}'
            table.cell(1,0).text='{% vm %}{{ item.group }}';table.cell(1,1).text='{{ item.name }}'
            table.cell(2,0).text='{%tr endfor %}'
            context={'items':[{'group':'g','name':'A'},{'group':'g','name':'B'}]}
        elif kind=='hm':
            table=source.add_table(rows=1,cols=3)
            for cell,text in zip(table.rows[0].cells,['{%tc for item in items %}','{% hm %}{{ item }}','{%tc endfor %}']):cell.text=text
            context={'items':['A','B']}
        else:
            table=source.add_table(rows=1,cols=2);table.cell(0,0).text='{% '+kind+' value %}X';table.cell(0,1).text='Y'
            context={'value':'FF0000' if kind=='cellbg' else 2}
        blob=render(engine,source,context)
        with zipfile.ZipFile(io.BytesIO(blob)) as package:root=etree.fromstring(package.read('word/document.xml'))
        ns={'w':W}
        observation={'text':root.xpath('//w:t/text()',namespaces=ns),
            'spans':root.xpath('//w:gridSpan/@w:val',namespaces=ns),
            'fills':root.xpath('//w:shd/@w:fill',namespaces=ns),
            'merges':root.xpath('//w:vMerge/@w:val',namespaces=ns)}
        return Result(observation,{'document.docx':blob})
    return execute

PROBES={
 'tag_paragraph_true':Probe(paragraph_control(True),{'paragraphs':['inside','after']}),
 'tag_paragraph_false':Probe(paragraph_control(False),{'paragraphs':['after']}),
 'tag_run_true':Probe(run_control(True),{'text':'insideafter','bold':['inside']}),
 'tag_run_false':Probe(run_control(False),{'text':'after','bold':[]}),
 'tag_cellbg':Probe(table_tag('cellbg'),{'text':['X','Y'],'spans':[],'fills':['FF0000'],'merges':[]}),
 'tag_colspan':Probe(table_tag('colspan'),{'text':['X','Y'],'spans':['2'],'fills':[],'merges':[]}),
 'tag_hm':Probe(table_tag('hm'),{'text':['A'],'spans':['2'],'fills':[],'merges':[]}),
 'tag_vm':Probe(table_tag('vm'),{'text':['g','A','B'],'spans':[],'fills':[],'merges':['restart','continue']}),
}
