"""Differential probes for the partitions of docxtpl's public API.

Every public method of the catalog (``DocxTemplate`` construction, render,
save, replacements, hyperlinks, subdocuments, analysis, and the rich value
classes) has the partitions normal, boundary, invalid input, exceptions,
identity and mutation, lifecycle order and interactions. Each probe runs one
scenario and records what users observe: the call's result or exception, the
template's state and the saved document.

Expected observations in ``api_partition_expected.json`` were recorded from
the reference (docxtpl 0.20.2) by ``scripts/record_reference_observations.py``.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jinja2
from docx import Document

from .probe_images import image
from .protocol_base import Probe, Result

PNG = image('red')
OTHER_PNG = image('blue')


class Failing:
    """An argument whose protocols fail."""
    def _fail(self, *args: Any, **kwargs: Any) -> Any:
        raise ValueError('probe argument failure')
    __str__ = __iter__ = __len__ = __getitem__ = __bool__ = __fspath__ = __index__ = read = keys = _fail


class FailingStream(io.BytesIO):
    def read(self, *args: Any) -> bytes:
        raise ValueError('probe stream failure')


def summary(value: Any) -> Any:
    """A value as both engines can be compared on it."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return re.sub(r' at 0x[0-9a-fA-F]+', '', value)
    if isinstance(value, (bytes, bytearray)):
        return {'bytes': hashlib.sha1(bytes(value)).hexdigest()}
    if isinstance(value, (set, frozenset)):
        return sorted(summary(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [summary(item) for item in value]
    if isinstance(value, dict):
        return {str(key): summary(item) for key, item in value.items()}
    return {'type': type(value).__name__}


def attempt(call: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {'value': summary(call())}
    except Exception as error:  # noqa: BLE001 - the exception is the observation
        return {'type': type(error).__module__ + '.' + type(error).__qualname__,
                'message': re.sub(r' at 0x[0-9a-fA-F]+', '', str(error))}


def source(text: str = 'Hi {{ name }}', picture: bool = False) -> bytes:
    document = Document()
    document.add_paragraph(text)
    if picture:
        document.add_picture(io.BytesIO(PNG))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def template(engine: Any, text: str = 'Hi {{ name }}', picture: bool = False) -> Any:
    return engine.DocxTemplate(io.BytesIO(source(text, picture)))


def saved(document: Any) -> dict[str, Any]:
    """The saved document's paragraphs and media, or the save's exception."""
    output = io.BytesIO()
    result = attempt(lambda: document.save(output))
    if 'type' in result:
        return result
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as package:
        media = {name: hashlib.sha1(package.read(name)).hexdigest()
                 for name in package.namelist() if name.startswith('word/media/')}
    parsed = Document(io.BytesIO(output.getvalue()))
    return {'paragraphs': [paragraph.text for paragraph in parsed.paragraphs], 'media': media}


def rendered(document: Any, context: Any = None) -> dict[str, Any]:
    result = attempt(lambda: document.render({'name': 'Ada'} if context is None else context))
    return result if 'type' in result else saved(document)


def state(document: Any) -> dict[str, Any]:
    return {'rendered': document.is_rendered, 'saved': document.is_saved}


def files(*contents: bytes) -> list[str]:
    """Temporary files holding `contents`, removed when the process exits."""
    paths = []
    for content in contents:
        handle, path = tempfile.mkstemp(suffix='.png')
        os.write(handle, content)
        os.close(handle)
        paths.append(path)
    return paths


Scenario = Callable[[Any], dict[str, Any]]


def _constructor() -> dict[str, Scenario]:
    def boundary(engine: Any) -> dict[str, Any]:
        (path,) = files(source())
        return {'str': rendered(engine.DocxTemplate(path)), 'pathlike': rendered(engine.DocxTemplate(Path(path)))}

    def identity(engine: Any) -> dict[str, Any]:
        stream = io.BytesIO(source())
        document = engine.DocxTemplate(stream)
        return {'same': document.template_file is stream, 'output': rendered(document)}

    def interactions(engine: Any) -> dict[str, Any]:
        data = source()
        first, second = engine.DocxTemplate(io.BytesIO(data)), engine.DocxTemplate(io.BytesIO(data))
        return {'first': rendered(first, {'name': 'A'}), 'second': rendered(second, {'name': 'B'})}
    return {
        'normal': lambda engine: rendered(template(engine)),
        'boundary': boundary,
        'invalid_input': lambda engine: rendered(engine.DocxTemplate(object())),
        'exceptions': lambda engine: rendered(engine.DocxTemplate(FailingStream(source()))),
        'identity_mutation': identity,
        'lifecycle_order': lambda engine: saved(template(engine)),
        'interactions': interactions,
    }


def _init_docx() -> dict[str, Scenario]:
    def boundary(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.init_docx(reload=False)
        first = document.docx
        document.init_docx(reload=False)
        return {'same': document.docx is first}

    def identity(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.init_docx()
        first = document.docx
        document.init_docx()
        return {'same': document.docx is first}

    def lifecycle(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.render({'name': 'Ada'})
        document.init_docx(reload=True)
        return {'state': state(document), 'text': document.docx.paragraphs[0].text}

    def interactions(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.init_docx()
        document.docx.add_paragraph('{{ name }}!')
        return rendered(document)
    return {
        'boundary': boundary,
        'invalid_input': lambda engine: {'call': attempt(lambda: template(engine).init_docx(reload='x'))},
        'exceptions': lambda engine: {'call': attempt(lambda: template(engine).init_docx(reload=Failing()))},
        'identity_mutation': identity,
        'lifecycle_order': lifecycle,
        'interactions': interactions,
    }


def _getattr() -> dict[str, Scenario]:
    def interactions(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.init_docx()
        document.core_properties.title = 'T {{ name }}'
        output = io.BytesIO()
        document.render({'name': 'Ada'})
        document.save(output)
        return {'title': Document(io.BytesIO(output.getvalue())).core_properties.title}

    def boundary(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.init_docx()
        return {'paragraphs': len(document.__getattr__('paragraphs'))}

    def identity(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.init_docx()
        return {'same': document.part is document.docx.part}

    def lifecycle(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.render({'name': 'Ada'})
        return {'text': document.paragraphs[0].text}
    return {
        'boundary': boundary,
        'invalid_input': lambda engine: attempt(lambda: template(engine).__getattr__('no_such_attribute')),
        'exceptions': lambda engine: attempt(lambda: template(engine).__getattr__(Failing())),
        'identity_mutation': identity,
        'lifecycle_order': lifecycle,
        'interactions': interactions,
    }


def _get_docx() -> dict[str, Scenario]:
    def lifecycle(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.render({'name': 'Ada'})
        model = document.get_docx()
        return {'state': state(document), 'text': model.paragraphs[0].text}

    def interactions(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.get_docx().add_paragraph('{{ name }}!')
        return rendered(document)

    def boundary(engine: Any) -> dict[str, Any]:
        document = template(engine)
        return {'same': document.get_docx() is document.get_docx()}

    def identity(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.get_docx().add_paragraph('X')
        return {'same': document.docx is document.get_docx(), 'count': len(document.get_docx().paragraphs)}
    return {
        'normal': lambda engine: {'text': template(engine).get_docx().paragraphs[0].text},
        'boundary': boundary,
        'invalid_input': lambda engine: attempt(lambda: template(engine).get_docx(1)),
        'exceptions': lambda engine: attempt(lambda: engine.DocxTemplate(FailingStream(source())).get_docx()),
        'identity_mutation': identity,
        'lifecycle_order': lifecycle,
        'interactions': interactions,
    }


def _render() -> dict[str, Scenario]:
    def identity(engine: Any) -> dict[str, Any]:
        context = {'name': 'Ada', 'items': [1, 2]}
        document = template(engine, 'Hi {{ name }} {{ items|length }}')
        document.render(context)
        return {'context': summary(context), 'state': state(document)}

    def lifecycle(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.render({'name': 'A'})
        return rendered(document, {'name': 'B'})

    def escaped(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.render({'name': '<b>'}, autoescape=True)
        return saved(document)

    def interactions(engine: Any) -> dict[str, Any]:
        document = template(engine, '{% for item in items %}{{ item }}{% endfor %}')
        document.render({'items': ['a', 'b']}, jinja_env=jinja2.Environment(trim_blocks=True))
        return saved(document)
    return {
        'normal': lambda engine: rendered(template(engine)),
        'boundary': lambda engine: {'missing': rendered(template(engine), {}),
                                    'escaped': attempt(lambda: escaped(engine))},
        'invalid_input': lambda engine: {'list': rendered(template(engine), [1]),
                                         'none': rendered(template(engine), None)},
        'exceptions': lambda engine: rendered(template(engine), {'name': Failing()}),
        'identity_mutation': identity,
        'lifecycle_order': lifecycle,
        'interactions': interactions,
    }


def _new_subdoc() -> dict[str, Scenario]:
    def normal(engine: Any) -> dict[str, Any]:
        document = template(engine, '{{p sub }}')
        sub = document.new_subdoc()
        sub.add_paragraph('Inside')
        return rendered(document, {'sub': sub})

    def interactions(engine: Any) -> dict[str, Any]:
        document = template(engine, '{{p a }}')
        document.get_docx().add_paragraph('{{p b }}')
        first, second = document.new_subdoc(), document.new_subdoc()
        first.add_paragraph('First')
        second.add_paragraph('Second')
        return rendered(document, {'a': first, 'b': second})

    def lifecycle(engine: Any) -> dict[str, Any]:
        document = template(engine, '{{p sub }}')
        document.render({'sub': ''})
        return {'call': attempt(lambda: document.new_subdoc()), 'state': state(document)}

    def identity(engine: Any) -> dict[str, Any]:
        document = template(engine)
        return {'same': document.new_subdoc().tpl is document}
    return {
        'normal': normal,
        'boundary': lambda engine: {'type': type(template(engine).new_subdoc(None)).__name__},
        'invalid_input': lambda engine: attempt(lambda: template(engine).new_subdoc(object())),
        'exceptions': lambda engine: attempt(lambda: template(engine).new_subdoc(Failing())),
        'identity_mutation': identity,
        'lifecycle_order': lifecycle,
        'interactions': interactions,
    }


def _get_file_crc() -> dict[str, Scenario]:
    def identity(engine: Any) -> dict[str, Any]:
        stream = io.BytesIO(b'prefix123456789')
        stream.seek(6)
        value = engine.DocxTemplate.get_file_crc(stream)
        return {'crc': value, 'position': stream.tell()}

    def interactions(engine: Any) -> dict[str, Any]:
        (path,) = files(b'123456789')
        return {'path': engine.DocxTemplate.get_file_crc(path),
                'stream': engine.DocxTemplate.get_file_crc(io.BytesIO(b'123456789'))}
    return {
        'normal': lambda engine: attempt(lambda: engine.DocxTemplate.get_file_crc(io.BytesIO(b'123456789'))),
        'boundary': lambda engine: attempt(lambda: engine.DocxTemplate.get_file_crc(io.BytesIO(b''))),
        'invalid_input': lambda engine: attempt(lambda: engine.DocxTemplate.get_file_crc(1.5)),
        'exceptions': lambda engine: attempt(lambda: engine.DocxTemplate.get_file_crc(FailingStream(b'x'))),
        'identity_mutation': identity,
        'lifecycle_order': lambda engine: attempt(lambda: template(engine).get_file_crc(io.BytesIO(b'123456789'))),
        'interactions': interactions,
    }


def _replacement(method: str) -> dict[str, Scenario]:
    def call(document: Any, old: Any, new: Any) -> dict[str, Any]:
        return attempt(lambda: getattr(document, method)(old, new))

    def arguments(document: Any) -> tuple[Any, Any]:
        old_path, new_path = files(PNG, OTHER_PNG)
        if method == 'replace_media':
            return old_path, new_path
        if method == 'replace_pic':
            return 'image.png', new_path
        if method == 'replace_embedded':
            return old_path, new_path
        return 'word/media/image1.png', new_path

    def normal(engine: Any) -> dict[str, Any]:
        document = template(engine, picture=True)
        old, new = arguments(document)
        return {'call': call(document, old, new), 'output': rendered(document)}

    def boundary(engine: Any) -> dict[str, Any]:
        document = template(engine, picture=True)
        old, _ = arguments(document)
        same = files(PNG)[0]
        return {'call': call(document, old, same), 'output': rendered(document)}

    def identity(engine: Any) -> dict[str, Any]:
        document = template(engine, picture=True)
        old, new = arguments(document)
        call(document, old, new)
        maps = {'media': document.crc_to_new_media, 'embedded': document.crc_to_new_embedded,
                'zipname': document.zipname_to_replace, 'pictures': document.pics_to_replace}
        return {name: len(mapping) for name, mapping in maps.items()}

    def lifecycle(engine: Any) -> dict[str, Any]:
        document = template(engine, picture=True)
        document.render({'name': 'Ada'})
        old, new = arguments(document)
        return {'call': call(document, old, new), 'output': saved(document)}

    def interactions(engine: Any) -> dict[str, Any]:
        document = template(engine, picture=True)
        old, new = arguments(document)
        call(document, old, new)
        document.reset_replacements()
        call(document, old, new)
        return rendered(document)

    def rejected(old: Callable[[], Any], new: Callable[[], Any]) -> Scenario:
        def scenario(engine: Any) -> dict[str, Any]:
            document = template(engine, picture=True)
            return {'call': call(document, old(), new()), 'output': rendered(document)}
        return scenario
    return {
        'normal': normal,
        'boundary': boundary,
        # Not ints: a file descriptor would be opened, and closed, as a path.
        'invalid_input': rejected(lambda: 1.5, lambda: 2.5),
        'exceptions': rejected(Failing, Failing),
        'identity_mutation': identity,
        'lifecycle_order': lifecycle,
        'interactions': interactions,
    }


def _reset_replacements() -> dict[str, Scenario]:
    def normal(engine: Any) -> dict[str, Any]:
        document = template(engine, picture=True)
        document.replace_zipname('word/media/image1.png', files(OTHER_PNG)[0])
        document.reset_replacements()
        return rendered(document)

    def identity(engine: Any) -> dict[str, Any]:
        document = template(engine)
        before = document.crc_to_new_media
        document.reset_replacements()
        return {'same_mapping': document.crc_to_new_media is before}

    def boundary(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.reset_replacements()
        document.reset_replacements()
        return rendered(document)

    def lifecycle(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.render({'name': 'Ada'})
        document.reset_replacements()
        return saved(document)

    def interactions(engine: Any) -> dict[str, Any]:
        document = template(engine, picture=True)
        document.replace_zipname('word/media/image1.png', files(OTHER_PNG)[0])
        document.reset_replacements()
        document.replace_zipname('word/media/image1.png', files(OTHER_PNG)[0])
        return rendered(document)
    return {
        'normal': normal,
        'boundary': boundary,
        'invalid_input': lambda engine: attempt(lambda: template(engine).reset_replacements(1)),
        'exceptions': lambda engine: attempt(lambda: template(engine).reset_replacements(Failing())),
        'identity_mutation': identity,
        'lifecycle_order': lifecycle,
        'interactions': interactions,
    }


def _build_url_id() -> dict[str, Scenario]:
    def normal(engine: Any) -> dict[str, Any]:
        document = template(engine)
        return {'id': document.build_url_id('https://example.com'), 'output': rendered(document)}

    def lifecycle(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.render({'name': 'Ada'})
        return {'id': attempt(lambda: document.build_url_id('https://example.com')), 'state': state(document)}

    def interactions(engine: Any) -> dict[str, Any]:
        document = template(engine, 'Hi {{ name }}')
        url = document.build_url_id('https://example.com')
        rich = engine.RichText('link', url_id=url)
        return rendered(document, {'name': rich})

    def repeated(url: str) -> Scenario:
        def scenario(engine: Any) -> dict[str, Any]:
            document = template(engine)
            return {'ids': [document.build_url_id(url), document.build_url_id(url)]}
        return scenario
    return {
        'normal': normal,
        'boundary': repeated(''),
        'invalid_input': lambda engine: attempt(lambda: template(engine).build_url_id(1)),
        'exceptions': lambda engine: attempt(lambda: template(engine).build_url_id(Failing())),
        'identity_mutation': repeated('https://a'),
        'lifecycle_order': lifecycle,
        'interactions': interactions,
    }


def _save() -> dict[str, Scenario]:
    def boundary(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.render({'name': 'Ada'})
        handle, path = tempfile.mkstemp(suffix='.docx')
        os.close(handle)
        result = attempt(lambda: document.save(Path(path)))
        return {'call': result, 'text': Document(path).paragraphs[0].text if 'value' in result else None}

    def identity(engine: Any) -> dict[str, Any]:
        document = template(engine)
        document.render({'name': 'Ada'})
        first, second = io.BytesIO(), io.BytesIO()
        document.save(first)
        document.save(second)
        return {'equal': first.getvalue() == second.getvalue(), 'state': state(document)}

    def interactions(engine: Any) -> dict[str, Any]:
        document = template(engine, picture=True)
        document.render({'name': 'Ada'})
        document.replace_zipname('word/media/image1.png', files(OTHER_PNG)[0])
        return saved(document)

    def rejected(target: Callable[[], Any]) -> Scenario:
        def scenario(engine: Any) -> dict[str, Any]:
            document = template(engine)
            document.render({'name': 'Ada'})
            return attempt(lambda: document.save(target()))
        return scenario
    return {
        'normal': lambda engine: rendered(template(engine)),
        'boundary': boundary,
        'invalid_input': rejected(object),
        'exceptions': rejected(Failing),
        'identity_mutation': identity,
        'interactions': interactions,
    }


def _undeclared() -> dict[str, Scenario]:
    text = 'Hi {{ name }} {% for item in items %}{{ item.x }}{% endfor %}'

    def identity(engine: Any) -> dict[str, Any]:
        document = template(engine, text)
        document.get_undeclared_template_variables()
        return state(document)

    def lifecycle(engine: Any) -> dict[str, Any]:
        document = template(engine, text)
        document.render({'name': 'A', 'items': []})
        return attempt(document.get_undeclared_template_variables)
    return {
        'normal': lambda engine: attempt(lambda: template(engine, text).get_undeclared_template_variables()),
        'boundary': lambda engine: attempt(lambda: template(engine, 'plain').get_undeclared_template_variables()),
        'invalid_input': lambda engine: attempt(lambda: template(engine, text).get_undeclared_template_variables(
            jinja_env=1)),
        'exceptions': lambda engine: attempt(lambda: template(engine, text).get_undeclared_template_variables(
            context=Failing())),
        'identity_mutation': identity,
        'lifecycle_order': lifecycle,
        'interactions': lambda engine: attempt(lambda: template(engine, text).get_undeclared_template_variables(
            jinja_env=jinja2.Environment(), context={'name': 'A'})),
    }


def _value(cls: str, make: Callable[..., Any], method: str) -> dict[str, Scenario]:
    """A rich value's constructor, `add`, `__str__` or `__html__`."""
    def show(value: Any) -> dict[str, Any]:
        return {'str': attempt(lambda: str(value)), 'html': attempt(value.__html__)}

    def use(engine: Any, value: Any) -> dict[str, Any]:
        document = template(engine, '{{r value }}' if cls in ('RichText',) else '{{p value }}'
                            if cls in ('RichTextParagraph', 'Subdoc') else '{{ value }}')
        return rendered(document, {'value': value})

    construct = method == '__init__'
    add = method == 'add'

    def added(engine: Any, text: Any) -> dict[str, Any]:
        value = make(engine)
        value.add(text)
        return show(value)

    def normal(engine: Any) -> dict[str, Any]:
        return show(make(engine)) | {'output': use(engine, make(engine))}

    def boundary(engine: Any) -> dict[str, Any]:
        if construct and cls in ('RichText', 'RichTextParagraph'):
            return show(getattr(engine, cls)()) | show(getattr(engine, cls)(''))
        if construct and cls == 'Listing':
            return show(engine.Listing(''))
        if add:
            value = make(engine)
            value.add('')
            return show(value)
        return show(make(engine))

    def invalid(engine: Any) -> dict[str, Any]:
        if construct and cls in ('RichText', 'RichTextParagraph', 'Listing'):
            return attempt(lambda: show(getattr(engine, cls)(object())))
        if construct:
            return attempt(lambda: show(make(engine, object())))
        if add:
            return attempt(lambda: added(engine, 1))
        return attempt(lambda: getattr(make(engine), method)(1))

    def failing(engine: Any) -> dict[str, Any]:
        if construct and cls in ('RichText', 'RichTextParagraph', 'Listing'):
            return attempt(lambda: show(getattr(engine, cls)(Failing())))
        if construct:
            return attempt(lambda: show(make(engine, Failing())))
        if add:
            return attempt(lambda: added(engine, Failing()))
        return attempt(lambda: getattr(make(engine), method)(Failing()))

    def identity(engine: Any) -> dict[str, Any]:
        value = make(engine)
        first = attempt(lambda: str(value))
        if add:
            result = value.add('more')
            return {'returns_self': result is value, 'changed': attempt(lambda: str(value)) != first}
        return {'stable': attempt(lambda: str(value)) == first}

    def lifecycle(engine: Any) -> dict[str, Any]:
        value = make(engine)
        output = use(engine, value)
        if add:
            value.add('later')
        return {'first': output, 'after': show(value)}

    def interactions(engine: Any) -> dict[str, Any]:
        value = make(engine)
        if add:
            value.add(' plus', bold=True) if cls == 'RichText' else value.add(' plus')

        def escaped() -> dict[str, Any]:
            document = template(engine, '{{ value }}')
            document.render({'value': value}, autoescape=True)
            return saved(document)
        return {'autoescape': attempt(escaped)}
    return {'normal': normal, 'boundary': boundary, 'invalid_input': invalid, 'exceptions': failing,
            'identity_mutation': identity, 'lifecycle_order': lifecycle, 'interactions': interactions}


def _inline_image(engine: Any, descriptor: Any = None) -> Any:
    document = template(engine, '{{ value }}')
    return engine.InlineImage(document, io.BytesIO(PNG) if descriptor is None else descriptor)


def _inline_image_class(engine: Any) -> dict[str, Any]:
    """InlineImage keeps its identity, and a subclass inherits its rendering."""
    class Framed(engine.InlineImage):
        pass
    document = template(engine, '{{ value }}')
    image = Framed(document, io.BytesIO(PNG))
    return {'name': engine.InlineImage.__name__, 'module': engine.InlineImage.__module__.split('.')[-1],
            'instance': isinstance(image, engine.InlineImage), 'same_template': image.tpl is document,
            'output': rendered(document, {'value': image})}


def _subdoc(engine: Any, docpath: Any = None) -> Any:
    document = template(engine, '{{p value }}')
    if docpath is None:
        sub = document.new_subdoc()
        sub.add_paragraph('Inside')
        return sub
    return engine.Subdoc(document, docpath)


def _scenarios() -> dict[str, dict[str, Scenario]]:
    table: dict[str, dict[str, Scenario]] = {
        'DocxTemplate.__init__': _constructor(),
        'DocxTemplate.init_docx': _init_docx(),
        'DocxTemplate.__getattr__': _getattr(),
        'DocxTemplate.get_docx': _get_docx(),
        'DocxTemplate.render': _render(),
        'DocxTemplate.new_subdoc': _new_subdoc(),
        'DocxTemplate.get_file_crc': _get_file_crc(),
        'DocxTemplate.reset_replacements': _reset_replacements(),
        'DocxTemplate.build_url_id': _build_url_id(),
        'DocxTemplate.save': _save(),
        'DocxTemplate.get_undeclared_template_variables': _undeclared(),
    }
    for method in ('replace_media', 'replace_pic', 'replace_embedded', 'replace_zipname'):
        table['DocxTemplate.' + method] = _replacement(method)
    values: dict[str, Callable[..., Any]] = {
        'RichText': lambda engine, text='Rich': engine.RichText(text, bold=True),
        'RichTextParagraph': lambda engine, text='Para': engine.RichTextParagraph(text),
        'Listing': lambda engine, text='a\nb\tc & <d>': engine.Listing(text),
        'InlineImage': _inline_image,
        'Subdoc': _subdoc,
    }
    methods = {'RichText': ('__init__', 'add', '__str__', '__html__'),
               'RichTextParagraph': ('__init__', 'add', '__str__', '__html__'),
               'Listing': ('__init__', '__str__', '__html__'), 'InlineImage': ('__init__', '__str__', '__html__'),
               'Subdoc': ('__init__', '__getattr__', '__str__', '__html__')}
    for cls, names in methods.items():
        for method in names:
            table[cls + '.' + method] = _value(cls, values[cls], method)
    table['InlineImage.__init__']['class_identity_and_reference_inheritance'] = _inline_image_class
    return table


SCENARIOS = _scenarios()


def probe_name(contract: str, partition: str) -> str:
    return 'api_partition_' + contract.replace('.', '_').strip('_') + '_' + partition


def _execution(scenario: Scenario) -> Callable[[Any], Result]:
    def execute(engine: Any) -> Result:
        return Result(json.loads(json.dumps(attempt(lambda: scenario(engine)), default=str)), {})
    return execute


EXECUTIONS = {probe_name(contract, partition): _execution(scenario)
              for contract, partitions in SCENARIOS.items() for partition, scenario in partitions.items()}
EXPECTED_PATH = Path(__file__).with_name('api_partition_expected.json')
EXPECTED: dict[str, Any] = json.loads(EXPECTED_PATH.read_text()) if EXPECTED_PATH.exists() else {}
PROBES: dict[str, Probe] = {name: Probe(execute, EXPECTED[name], True)
                            for name, execute in EXECUTIONS.items() if name in EXPECTED}
