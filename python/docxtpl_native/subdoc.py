"""Bindings for the exposed subdocument composer."""
from docx import Document
from docx.oxml import CT_SectPr

# The composer bindings read the globals of docxcompose's Composer, which keeps
# them even if its module is later removed from sys.modules.
from docxcompose.composer import Composer
from docxcompose.properties import CustomProperties
from lxml import etree  # ty: ignore[import-not-found]

from . import Subdoc, _extension

# Names the native extension reads from this module's globals.
_NATIVE_GLOBALS = (Document, CT_SectPr, Composer, CustomProperties, etree, Subdoc)

NS, RT = _extension().composer_constants()


def _initialize_programmatic(subdoc, tpl):
    _extension().init_programmatic_subdoc(subdoc, tpl)


def _initialize_file(subdoc, tpl, source):
    _extension().init_file_subdoc(subdoc, tpl, source)


def _programmatic_xml(subdoc, unsupported_class):
    return _extension().programmatic_subdoc_xml(subdoc, unsupported_class)


class SubdocComposer:
    def __init__(self, doc):
        _extension().init_composer(self, doc)

    def reset_reference_mapping(self):
        _extension().reset_composer(self)

    def attach_parts(self, doc, remove_property_fields=True):
        return _extension().composer_attach_parts(self, doc, remove_property_fields)

    def add_diagrams(self, doc, element):
        return _extension().composer_add_diagrams(self, doc, element, globals(), './/dgm:relIds[@r:dm]')

    def fix_section_types(self, doc):
        return _extension().composer_fix_section_types(self, doc)

    def remove_header_and_footer_references(self, doc, element):
        return _extension().composer_remove_header_and_footer_references(element, './/w:headerReference|.//w:footerReference')

    def renumber_bookmarks(self):
        return _extension().composer_renumber_bookmarks(self, './/w:bookmarkStart', './/w:bookmarkEnd')

    def renumber_docpr_ids(self):
        return _extension().composer_renumber_drawing_ids(self, './/wp:docPr')

    def renumber_nvpicpr_ids(self):
        return _extension().composer_renumber_drawing_ids(self, './/pic:cNvPr')

    def add_referenced_parts(self, src_part, dst_part, element):
        return _extension().composer_add_referenced_parts(self, src_part, dst_part, element, './/*[@r:id]')

    def add_relationship(self, src_part, dst_part, relationship):
        return _extension().composer_add_relationship(self, dst_part, relationship)

    def add_images(self, doc, element):
        return _extension().composer_add_images(self, doc, element, '(.//a:blip|.//asvg:svgBlip)[@r:embed]')

    def add_shapes(self, doc, element):
        return _extension().composer_add_shapes(self, doc, element, './/v:shape/v:imagedata')

    def add_styles_from_other_parts(self, doc):
        return _extension().composer_add_styles_from_other_parts(self, doc)

    def _insert_num(self, element):
        return _extension().composer_insert_numbering(self, element, './/w:num', False)

    def _insert_abstract_num(self, element):
        return _extension().composer_insert_numbering(self, element, './/w:num', True)

    def _replace_mapped_num_id(self, old_id, new_id):
        return _extension().composer_replace_mapped_num_id(self, old_id, new_id)

    def numbering_part(self):
        return _extension().composer_numbering_part(self, '/word/numbering.xml', 'templates', 'numbering.xml', 'rb')

    def footnote_part(self):
        return _extension().composer_footnote_part(self, '/word/footnotes.xml', 'templates', 'footnotes.xml', 'rb')

    def add_footnotes(self, doc, element):
        return _extension().composer_add_footnotes(self, doc, element, './/w:footnoteReference', './/w:footnote[@w:id="%s"]')

    def _next_numbering_ids(self):
        return _extension().composer_next_numbering_ids(self, './/w:num', './/w:abstractNum/@w:abstractNumId')

    def add_numberings(self, doc, element):
        return _extension().composer_add_numberings(self, doc, element, './/w:numId', './/w:num[@w:numId="%s"]', '//w:abstractNumId', './/w:abstractNum[@w:abstractNumId="%s"]', './/w:nsid')

    def add_styles(self, doc, element):
        return _extension().composer_add_styles(self, doc, element, './/w:tblStyle|.//w:pStyle|.//w:rStyle', './/w:link/@w:val', './/w:numId/@w:val', './/w:num[@w:numId="%s"]/w:abstractNumId/@w:val', './/w:tblStyle[@w:val="%(styleid)s"]|.//w:pStyle[@w:val="%(styleid)s"]|.//w:rStyle[@w:val="%(styleid)s"]')

    def restart_first_numbering(self, doc, element):
        return _extension().composer_restart_first_numbering(self, doc, element, ('.//w:pStyle/@w:val', './/w:outlineLvl', './/w:numPr/w:numId/@w:val', './/w:numId/@w:val', './/w:num[@w:numId="%s"]', './/w:abstractNumId/@w:val', './/w:abstractNum[@w:abstractNumId="%s"]', './/w:lvl[@w:ilvl="0"]/w:numFmt/@w:val', '<w:lvlOverride xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride>', './/w:pPr/w:pStyle[@w:val="%s"]/parent::w:pPr', './/w:numPr', '<w:numPr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:ilvl w:val="0"/><w:numId w:val="%s"/></w:numPr>'))

    def _create_style_id_mapping(self, doc):
        return _extension().composer_create_style_id_mapping(self, doc)

    def mapped_style_id(self, style_id):
        return _extension().composer_mapped_style_id(self, style_id)
