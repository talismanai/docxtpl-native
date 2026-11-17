"""Observable save phases on the exposed document graph."""
import io
from unittest.mock import patch

from docx import Document
from PIL import Image
import jinja2
from .protocol_base import Probe, Result, observed, template


def live_save_hooks(engine):
    document = engine.DocxTemplate(io.BytesIO(template('Hello {{ name }}')))
    document.render({'name': 'Ada'}, jinja_env=jinja2.Environment())
    model = document.docx
    target = io.BytesIO()
    trace = []
    def before():
        trace.append(['before', document.docx is model, document.is_saved])
        model.paragraphs[0].text = 'Changed before save'
    def after(output):
        trace.append(['after', output is target, document.is_saved])
    original = type(model).save
    def save(model, output):
        trace.append(['model', output is target])
        return original(model, output)
    document.pre_processing = before
    document.post_processing = after
    with patch.object(type(model), 'save', save):
        document.save(target)
    return observed(target.getvalue(), trace, saved=document.is_saved)


def live_save_stream_error(engine):
    document = engine.DocxTemplate(io.BytesIO(template('Hello {{ name }}')))
    document.render({'name': 'Ada'}, jinja_env=jinja2.Environment())
    failure = ValueError('write failed')
    writes = 0
    class Stream(io.BytesIO):
        def write(self, value):
            nonlocal writes
            writes += 1
            if writes == 4:
                raise failure
            return super().write(value)
    target = Stream()
    identical = False
    try:
        document.save(target)
    except ValueError as error:
        identical = error is failure
    observation = {'writes': writes, 'original_error': identical,
                   'saved': document.is_saved, 'cursor': target.tell(),
                   'partial_zip': target.getvalue().startswith(b'PK\x03\x04')}
    failure.__traceback__ = None
    return Result(observation, {})


def live_save_picture_state(engine):
    def png(color):
        output = io.BytesIO()
        Image.new('RGB', (2, 2), color).save(output, format='PNG')
        return output.getvalue()
    source = Document()
    source.add_paragraph().add_run().add_picture(io.BytesIO(png('red')))
    buffer = io.BytesIO()
    source.save(buffer)
    document = engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
    document.render({}, jinja_env=jinja2.Environment())
    retained = next(rel.target_part for rel in document.docx.part.rels.values()
                    if rel.reltype.endswith('/image'))
    trace = []
    for color in ('blue', 'green'):
        content = png(color)
        document.pics_to_replace = {'image.png': content}
        output = io.BytesIO()
        document.save(output)
        trace.append([retained.blob == content, sorted(document.pic_map)])
    return observed(output.getvalue(), trace, saved=document.is_saved)


PROBES = {
    'live_save_hooks': Probe(live_save_hooks, {
        'text': 'Changed before save', 'trace': [['before', True, False],
        ['model', True], ['after', True, False]], 'saved': True}),
    'live_save_stream_error': Probe(live_save_stream_error, {
        'writes': 4, 'original_error': True, 'saved': False, 'cursor': 0,
        'partial_zip': True}, assertion_only=True),
    'live_save_picture_state': Probe(live_save_picture_state, {
        'text': '', 'trace': [[True, ['image.png']], [True, ['image.png']]],
        'saved': True}),
}
