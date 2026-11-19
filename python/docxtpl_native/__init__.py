"""Python interface to the native DOCX template engine.

The classes are this package's own; docxtpl is not imported. Missing features
fail explicitly with UnsupportedFeature; there is no fallback to docxtpl.
See docs/API.md.
"""
from __future__ import annotations

import io
import json
import math
import os
from pathlib import Path

from docx import Document
from docx.opc.oxml import parse_xml
from jinja2 import Environment, meta

# Names the native extension reads from this module's globals.
_NATIVE_GLOBALS = (math, os, Document, parse_xml, Environment, meta)


def _plain_environment(environment: object) -> bool:
    """Whether an explicit environment runs no user code while rendering: a
    plain synchronous ``Environment`` whose filters, tests, globals and
    policies are the defaults, with no extensions, loader, ``finalize``,
    replaced method or custom undefined class. Such an environment renders
    like the default one, so it needs neither the live document model nor
    the reference parser for the native frontend's grammar (syntax and the
    parser are still checked by ``BridgeConfig`` and for each source)."""
    if type(environment) is not Environment:
        return False
    from jinja2 import ChainableUndefined, DebugUndefined, StrictUndefined, Undefined
    baseline = Environment()
    return (not environment.is_async and not environment.extensions and environment.finalize is None
            and environment.loader is None and environment.bytecode_cache is None
            and type(environment.autoescape) is bool
            and environment.undefined in (Undefined, ChainableUndefined, DebugUndefined, StrictUndefined)
            and set(vars(environment)) == set(vars(baseline))
            and environment.filters == baseline.filters and environment.tests == baseline.tests
            and environment.globals == baseline.globals and environment.policies == baseline.policies)

__version__ = "0.2.1"

class UnsupportedFeature(NotImplementedError):
    pass

_native_extension = None

def _extension():
    global _native_extension
    if _native_extension is None:
        try:
            from . import _native  # ty: ignore[import-not-found] - the compiled extension
        except ImportError as exc:
            raise RuntimeError("Native extension unavailable. Build with: maturin develop --release") from exc
        _native_extension = _native
    return _native_extension



class _RichValueBinding:
    xml: str  # set by the extension

    def __init__(self, text=None, **text_prop):
        _extension().init_rich_value(self, text, text_prop)


class RichText(_RichValueBinding):
    """Runs of formatted text, inserted with ``{{r value }}``."""

    def add(self, text, style=None, color=None, highlight=None, size=None,
            subscript=None, superscript=None, bold=False, italic=False,
            underline=False, strike=False, font=None, url_id=None, rtl=False,
            lang=None):
        return _extension().add_richtext(self, text,
            (style, color, highlight, size, subscript, superscript, bold, italic,
             underline, strike, font, url_id, rtl, lang), RichText)

    # Each class owns its representation, as docxtpl's do (their names show
    # in errors).
    def __unicode__(self): return self.xml
    def __str__(self): return self.xml
    def __html__(self): return self.xml


R = RichText


class RichTextParagraph(_RichValueBinding):
    """Formatted paragraphs, inserted with ``{{p value }}``."""

    def add(self, text, parastyle=None):
        return _extension().add_richtext_paragraph(self, text, parastyle, RichText, RichText)

    def __unicode__(self): return self.xml
    def __str__(self): return self.xml
    def __html__(self): return self.xml


RP = RichTextParagraph

class Listing:
    """Preserve template styling while escaping text for the native renderer."""

    def __init__(self, text):
        self.xml = _extension().escape_listing(text)

    def __unicode__(self): return self.xml
    def __str__(self): return self.xml
    def __html__(self): return self.xml

# The package's own text values with their original formatting methods: an
# exact instance holding only its `xml` string runs no user code when shown.
_PLAIN_VALUE_TYPES = tuple((cls, cls.__str__, cls.__html__) for cls in (RichText, RichTextParagraph, Listing))

# Imported after _extension, which inline_image needs. The save guard
# snapshots python-docx's save chain when it is imported: at package import,
# before user code can replace part of that chain.
from . import _save_guard
from .inline_image import InlineImage

_LOADED_WITH_PACKAGE = (_save_guard,)


def _read(source):
    return _extension().read_source(source)

def _write_output(filename, data):
    # As zipfile.ZipFile(filename, 'w'): a path-like object's path, a str
    # path, or else any object, whose write the archive calls.
    if isinstance(filename, os.PathLike):
        filename = os.fspath(filename)
    if isinstance(filename, str):
        Path(filename).write_bytes(data)
    else:
        filename.write(data)

def _b64(value): return _extension().base64_encode(value)

class Subdoc:
    def __init__(self, tpl, docpath=None):
        if docpath is None:
            from .subdoc import _initialize_programmatic
            _initialize_programmatic(self, tpl)
        else:
            self.tpl, self.source = tpl, docpath
            from .subdoc import _initialize_file
            _initialize_file(self, tpl, docpath)

    def __getattr__(self, name):
        if name == 'subdocx':
            raise AttributeError(name)
        return getattr(self.subdocx, name)

    def _get_xml(self):
        from .subdoc import _programmatic_xml
        return _programmatic_xml(self, UnsupportedFeature)

    def __unicode__(self): return self._get_xml()
    def __str__(self): return self._get_xml()
    def __html__(self): return self._get_xml()

_OriginalSubdoc = Subdoc
_OriginalInlineImage = InlineImage
_OriginalNativeImageStr = InlineImage.__str__
_OriginalNativeImageStrCode = InlineImage.__str__.__code__
_OriginalNativeImageHtml = InlineImage.__html__
_OriginalNativeImageHtmlCode = InlineImage.__html__.__code__
_OriginalNativeImageInsert = InlineImage._insert_image
_OriginalNativeImageInsertCode = InlineImage._insert_image.__code__

def _value(value, *, depth=0, active=None):
    return _extension().encode_value(value,depth,active)

_OriginalValue = _value
_OriginalValueCode = _value.__code__

class DocxTemplate:
    HEADER_URI = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/header'
    FOOTER_URI = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer'
    def __init__(self, template_file):
        self.template_file = template_file
        self.is_rendered = self.is_saved = False
        self.allow_missing_pics = False
        self._rendered = None
        self._rendered_replacements = None
        self._document = None
        self._staged_source = None
        self._render_depth = 0
        self.last_execution = None
        self.reset_replacements()

    def reset_replacements(self):
        self.crc_to_new_media = {}
        self.crc_to_new_embedded = {}
        self.zipname_to_replace = {}
        self.pics_to_replace = {}

    def _payload(self, context=None, *, zip_replacements=True):
        return json.dumps({"context": _value({} if context is None else context),
          "replacements": {"media":{str(k):_b64(v) for k,v in self.crc_to_new_media.items()} if zip_replacements else {},
          "embedded":{str(k):_b64(v) for k,v in self.crc_to_new_embedded.items()} if zip_replacements else {},
          "zipname":{k:_b64(v) for k,v in self.zipname_to_replace.items()} if zip_replacements else {},
          "pictures":{k:_b64(v) for k,v in self.pics_to_replace.items()}}},ensure_ascii=False,allow_nan=False)

    def render(self, context, jinja_env=None, autoescape=False):
        from .bridge import BridgeConfig, collect_specials, requires_live_model
        self._render_depth+=1
        try:
            plain = jinja_env is not None and _plain_environment(jinja_env)
            if self._document is not None or requires_live_model(context,jinja_env):
                from .live import render
                self.render_init()
                configuration = BridgeConfig(jinja_env,autoescape)
                # A plain environment runs no user code: the native frontend
                # serves a data-only context, and the body before any object
                # of the context is used (see enable_simple_sources).
                if jinja_env is None or plain:configuration.enable_simple_sources(context, objects=True)
                return render(self,context,configuration)
            source = self._staged_source.to_bytes() if self._staged_source is not None else _read(self.template_file)
            configuration = BridgeConfig(jinja_env, autoescape)
            configuration._native_internal_special_collection = True
            if jinja_env is None:configuration.enable_simple_sources(context)
            payload = json.loads(self._payload(zip_replacements=False))
            payload["specials"] = collect_specials(context, configuration)
            data, self.last_execution = _extension().process_python_docx(source, context, configuration,
                json.dumps(payload,ensure_ascii=False,allow_nan=False), autoescape, self)
            self.last_execution['native_frontend_sources']=configuration.native_frontend_sources
            self._rendered = bytes(data)
            self._rendered_replacements = self._payload(zip_replacements=False)
            self._document = self._staged_source = None
            self.is_rendered, self.is_saved = True, False
        finally:self._render_depth-=1

    def save(self, filename):
        if not self.is_saved and not self.is_rendered:
            self._staged_source=None
            if self._document is not None:
                from docx import Document
                self._document=Document(io.BytesIO(_read(self.template_file)))
        if self._document is not None:
            data = _extension().save_exposed_model(self, filename)
            if data is None:
                return
            # As the unexposed path: zipfile's timestamps and stream writes.
            writer = _extension().ZipTimestampWriter(data, filename if type(filename) is io.BytesIO else None)
            data = writer.finish()
            if data is not None:
                _write_output(filename, data)
            self.post_processing(filename)
            self.is_saved = True
            return
        if self._rendered is not None and self._rendered_replacements == self._payload(zip_replacements=False):
            data = self._rendered
        else:
            source=self._rendered
            if source is None:
                source = self._staged_source.to_bytes() if self._staged_source is not None else _read(self.template_file)
            data = bytes(_extension().process_docx(source,self._payload(zip_replacements=False),False,False,
                source_rendered=self._rendered is not None))
        if type(self) is DocxTemplate and self._document is None:
            # The binding frame owns the writer: failed-save tracebacks retain
            # it until release, matching the exposed stream's resource lifetime.
            writer = _extension().ZipTimestampWriter(data,filename if type(filename) is io.BytesIO else None)
            data = writer.finish()
        if data is not None:
            _write_output(filename, data)
        self.post_processing(filename)
        self.is_saved = True

    def get_docx(self):
        # Reference init_docx(reload=True) reloads the original template after
        # a render, even if rendered bytes have already been saved.
        if self._document is None and self._render_depth:
            raise UnsupportedFeature('first document access in native file-composition rendering needs the live graph adapter')
        if self._document is None or self.is_rendered:
            source = self._staged_source.to_bytes() if self._staged_source is not None and not self.is_rendered else _read(self.template_file)
            from docx import Document
            document = Document(io.BytesIO(source))
            self._document = document
            if self.is_rendered:
                self._rendered = self._rendered_replacements = None
                self._staged_source = None
            self.is_rendered = False
        return self._document

    @property
    def docx(self):
        if self._document is None and self._rendered is not None:
            from docx import Document
            self._document=Document(io.BytesIO(self._rendered))
        return self._document

    @docx.setter
    def docx(self,value):self._document=value

    def __getattr__(self,name):return getattr(self.docx,name)

    def init_docx(self,reload=True):
        if self._document is None or (self.is_rendered and reload):
            from docx import Document
            source=self._staged_source.to_bytes() if self._staged_source is not None and not self.is_rendered else _read(self.template_file)
            self._document=Document(io.BytesIO(source))
            if self.is_rendered:
                self._rendered=self._rendered_replacements=None
                self._staged_source=None
            self.is_rendered=False

    def render_init(self):
        return _extension().render_init(self,1000)

    def open_package(self):
        """A native copy of the document's package: the rendered document once
        rendered, the template before. Change it with ``write``, ``relate`` and
        ``add_image``, then make it the document with ``commit_package``."""
        self._check_package_owner()
        if self.is_rendered and self._rendered is not None:
            data = self._rendered
        elif self._staged_source is not None:
            data = self._staged_source.to_bytes()
        else:
            data = _read(self.template_file)
        return _extension().NativePackage(data)

    def commit_package(self, package):
        """Make the document the bytes of ``package``: the rendered document
        once rendered (replacements registered with ``replace_media`` and the
        others still apply when saving), the template to render before."""
        if type(package) is not _extension().NativePackage:
            raise TypeError('commit_package() needs a package from open_package()')
        self._check_package_owner()
        data = package.to_bytes()
        if self.is_rendered:
            self._rendered = data
        else:
            self._staged_source = _extension().HyperlinkSource(data)
        self.is_saved = False

    def _check_package_owner(self):
        # One mutable graph at a time: an exposed python-docx model owns the
        # document until it is saved and rendering is over.
        if self._render_depth:
            raise UnsupportedFeature('the document package cannot be opened or committed while it renders')
        if self._document is not None:
            raise UnsupportedFeature('the python-docx model owns this document; the native package is available '
                                     'when the model is not exposed')

    def new_subdoc(self, docpath=None): return Subdoc(self,docpath)

    def build_url_id(self,url):
        if self._document is None and type(url) is not str and self._render_depth:
            raise UnsupportedFeature('hyperlinks on an actively rendering document need the live part-graph adapter')
        if self._document is not None or type(url) is not str:
            # python-docx keeps a non-string target as is: the relationship
            # is shared by equal targets and only serializing it can fail.
            self.init_docx()
            assert self._document is not None
            return self._document._part.relate_to(url,
                'http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink',is_external=True)
        if self._render_depth:
            raise UnsupportedFeature('hyperlinks on an exposed or actively rendering document need the live part-graph adapter')
        if self.is_rendered:
            self._staged_source=None
            self._rendered=self._rendered_replacements=None
            self.is_rendered=False
        if self._staged_source is None:self._staged_source=_extension().HyperlinkSource(_read(self.template_file))
        return self._staged_source.relate(url)

    def xml_to_string(self, xml, encoding='unicode'):
        return _extension().python_xml_to_string(xml)

    def get_xml(self):
        return _extension().get_xml(self)

    def write_xml(self, filename):
        return _extension().write_xml(self,filename)

    def patch_xml(self, src_xml):return _extension().patch_xml(src_xml)

    def resolve_listing(self, xml):
        return _extension().resolve_listing_xml(xml)

    def render_xml_part(self, src_xml, part, context, jinja_env=None):
        return _extension().render_python_xml_part(self, src_xml, part, context, jinja_env)

    def build_xml(self, context, jinja_env=None):
        return _extension().build_python_xml(self, context, jinja_env)

    def render_properties(self, context, jinja_env=None):
        return _extension().render_exposed_properties(self, context, jinja_env)

    def render_footnotes(self, context, jinja_env=None):
        return _extension().render_exposed_footnotes(self, context, jinja_env)

    def get_headers_footers_encoding(self, xml):
        return _extension().headers_footers_encoding(xml)

    def get_headers_footers(self, uri):
        yield from _extension().header_footer_iter(self, uri)

    def build_headers_footers_xml(self, context, uri, jinja_env=None):
        yield from _extension().build_header_footer_iter(self, context, uri, jinja_env)

    def map_headers_footers_xml(self, relKey, xml):
        return _extension().map_python_header_footer(self, relKey, xml)

    def map_tree(self, tree):
        return _extension().map_python_tree(self, tree)

    def fix_tables(self, xml):
        return _extension().fix_exposed_tables(xml)

    def pre_processing(self):
        return _extension().pre_process(self)

    def post_processing(self, docx_file):
        return _extension().post_process(self, docx_file)

    def _replace_pics(self):
        return _extension().replace_pics(self)

    def _replace_docx_part_pics(self, part, replaced_pics):
        return _extension().replace_part_pics(self, part, replaced_pics)

    def get_part_xml(self, part):
        return _extension().python_part_xml(self, part)

    def fix_docpr_ids(self, tree):
        return _extension().fix_python_docpr_ids(self, tree)

    def get_pic_map(self):
        return self.pic_map

    def get_undeclared_template_variables(self,jinja_env=None,context=None):
        return _extension().undeclared_variables(self,jinja_env,context)
    @staticmethod
    def get_file_crc(source):
        return _extension().crc_file_source(source)
    def replace_media(self, src_file, dst_file):
        return _extension().replace_media(self,src_file,dst_file)
    def replace_embedded(self, src_file, dst_file):
        return _extension().replace_embedded(self,src_file,dst_file)
    def replace_pic(self, embedded_file, dst_file):
        return _extension().replace_pic(self,embedded_file,dst_file)
    def replace_zipname(self, zipname, dst_file):
        return _extension().replace_zipname(self,zipname,dst_file)

def compose(documents):
    return bytes(_extension().compose_docx([_read(x) for x in documents]))

def capabilities():
    try: stack = _extension().STACK
    except RuntimeError: return {"native_available":False, "reference_fallback":False}
    return {"native_available":True,"stack":stack,"reference_fallback":False,
            "contract":"experimental; see docs/CONTRACT.md"}

