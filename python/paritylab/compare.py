from __future__ import annotations
import hashlib
import io
import posixpath
import zipfile
from typing import Any
from lxml import etree

_ZIP_READ_ERRORS: tuple[type[Exception], ...] = (
    zipfile.BadZipFile, EOFError, OSError, RuntimeError, NotImplementedError,
)
_zip_zlib: Any = getattr(zipfile, 'zlib', None)
_zip_lzma: Any = getattr(zipfile, 'lzma', None)
if _zip_zlib is not None:
    _ZIP_READ_ERRORS += (_zip_zlib.error,)
if _zip_lzma is not None:
    _ZIP_READ_ERRORS += (_zip_lzma.LZMAError,)

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
XSI = 'http://www.w3.org/2001/XMLSchema-instance'
WORD_PACKAGE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

def _namespace_refs(node):
    """Prefix-valued metadata depends on namespace bindings, not just text."""
    result=[]
    for name,value in node.attrib.items():
        prefixes=None
        if name in {'{'+MC+'}Ignorable','{'+MC+'}MustUnderstand'} or (node.tag=='{'+MC+'}Choice' and name=='Requires'):
            prefixes=value.split()
        elif name in {'{'+MC+'}ProcessContent','{'+MC+'}PreserveElements','{'+MC+'}PreserveAttributes','{'+XSI+'}type'}:
            prefixes=[token.split(':',1)[0] if ':' in token else None for token in value.split()]
        if prefixes is not None:
            result.append((name,tuple((prefix,'http://www.w3.org/XML/1998/namespace'
                if prefix=='xml' else node.nsmap.get(prefix)) for prefix in prefixes)))
    return tuple(sorted(result))

def _node(node):
    """Namespace URI-based tree; keep all attributes, whitespace and comments.

    Prefix/attribute order and empty-element syntax are lexical differences;
    namespace bindings referenced by QName/prefix-valued metadata are retained.
    No ID, style, section, field, timestamp or content nodes are discarded.
    """
    if isinstance(node, etree._Comment):
        return ('comment', node.text, node.tail)
    if isinstance(node, etree._ProcessingInstruction):
        return ('pi', node.target, node.text, node.tail)
    return (node.tag, tuple(sorted(node.attrib.items())), node.text, node.tail, _namespace_refs(node),
            tuple(_node(child) for child in node))

def _story(root):
    paragraphs = []
    for paragraph in root.iter('{'+W+'}p'):
        tokens = []
        for el in paragraph.iter():
            if el.tag in ('{'+W+'}t','{'+W+'}delText','{'+W+'}instrText'):
                tokens.append(el.text or '')
            elif el.tag == '{'+W+'}tab': tokens.append('\t')
            elif el.tag == '{'+W+'}br': tokens.append('<BREAK:'+el.get('{'+W+'}type','textWrapping')+'>')
        paragraphs.append(''.join(tokens))
    return paragraphs

def _tree(root: etree._Element, source: bytes) -> tuple[object, ...]:
    before=[];node=root.getprevious()
    while node is not None:
        before.append(_node(node));node=node.getprevious()
    after=[];node=root.getnext()
    while node is not None:
        after.append(_node(node));node=node.getnext()
    info=root.getroottree().docinfo
    doctype=info.doctype
    # libxml's docinfo.doctype omits the internal subset, including entity,
    # element and notation declarations.  Do not claim equality when those
    # declarations differ: require exact XML bytes whenever a DTD is present.
    # This is deliberately stricter than the normal namespace-based comparison.
    # XML version and standalone status are document metadata, not element
    # attributes. An omitted standalone declaration has the same default as
    # "no"; encoding and declaration spelling remain lexical differences.
    return (doctype,source if doctype else None,tuple(reversed(before)),_node(root),tuple(after),
            (info.xml_version,bool(info.standalone)))

def _source_part(rels_name):
    if rels_name == '_rels/.rels': return ''
    parent, filename = posixpath.split(rels_name)
    return posixpath.join(posixpath.dirname(parent), filename[:-5])

def inspect_docx(data: bytes, *, _embedded_depth: int = 0) -> dict[str, Any]:
    result: dict[str, Any] = {'size_bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),
              'uncompressed_bytes':0,'parts':{},'errors':[]}
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        result['errors'].append('invalid ZIP: '+str(e));return result
    with archive:
        names=archive.namelist();name_set=set(names)
        if len(name_set)!=len(names): result['errors'].append('duplicate ZIP entries')
        for required in ['[Content_Types].xml','_rels/.rels','word/document.xml']:
            if required not in name_set:result['errors'].append('missing '+required)
        roots={}
        parser=etree.XMLParser(recover=False,resolve_entities=False,no_network=True,remove_blank_text=False)
        # OPC overrides take precedence over extension defaults. Discover them
        # before reading parts so a Word package need not have a .docx suffix.
        overrides_by_name: dict[str, str | None] = {}
        defaults_by_extension: dict[str, str | None] = {}
        try:
            type_root = etree.fromstring(archive.read('[Content_Types].xml'), parser)
            overrides_by_name = {entry.get('PartName', '').lstrip('/').lower(): entry.get('ContentType')
                                 for entry in type_root if entry.tag == '{'+CT+'}Override'}
            defaults_by_extension = {entry.get('Extension', '').lower(): entry.get('ContentType')
                                     for entry in type_root if entry.tag == '{'+CT+'}Default'}
        except (KeyError, etree.XMLSyntaxError, *_ZIP_READ_ERRORS):
            # The ordinary pass reports missing, malformed or unreadable parts.
            pass
        for name in names:
            if name.endswith('/'):continue
            try:
                blob=archive.read(name)
            except _ZIP_READ_ERRORS as error:
                result['errors'].append(f'{name}: ZIP entry read failed ({type(error).__name__}): {error}')
                continue
            result['uncompressed_bytes']+=len(blob)
            part={'size_bytes':len(blob),'sha256':hashlib.sha256(blob).hexdigest()}
            content_type = overrides_by_name.get(name.lower(), defaults_by_extension.get(name.rsplit('.',1)[-1].lower()))
            embedded_word = name.lower().endswith('.docx') or content_type == WORD_PACKAGE
            if embedded_word:
                part['embedded_docx'] = True
                # Equal embedded bytes do not prove that either package is valid.
                # Bound recursion and fail closed when a deeper package is unverified.
                if _embedded_depth >= 8:
                    result['errors'].append(f'{name}: embedded DOCX nesting limit exceeded')
                else:
                    nested=inspect_docx(blob,_embedded_depth=_embedded_depth+1)
                    result['errors'].extend(f'{name}: {error}' for error in nested['errors'])
            elif name.endswith(('.xml','.rels')):
                try:
                    root=etree.fromstring(blob,parser)
                    roots[name]=root
                    part['tree']=_tree(root,blob);part['story']=_story(root)
                except etree.XMLSyntaxError as e:result['errors'].append(f'{name}: {e}')
            result['parts'][name]=part
        for name,root in roots.items():
            if not name.endswith('.rels'):continue
            source=_source_part(name)
            for rel in root:
                if rel.tag!='{'+R+'}Relationship' or rel.get('TargetMode')=='External':continue
                target=rel.get('Target','').split('#',1)[0]
                if not target:continue
                resolved=posixpath.normpath(posixpath.join(posixpath.dirname(source),target)) if not target.startswith('/') else target.lstrip('/')
                if resolved not in name_set:result['errors'].append(f'dangling relationship: {name} -> {resolved}')
        content_types=roots.get('[Content_Types].xml')
        if content_types is not None:
            for name in result['parts']:
                if name=='[Content_Types].xml':continue
                if name.lower() not in overrides_by_name and name.rsplit('.',1)[-1].lower() not in defaults_by_extension:
                    result['errors'].append('missing content type: '+name)
    return result

def compare_docx(reference,candidate,*,inspect_embedded=True):
    a,b=inspect_docx(reference),inspect_docx(candidate)
    diffs=[];same_content=True;same_structure=True
    for name in sorted(set(a['parts'])|set(b['parts'])):
        left,right=a['parts'].get(name),b['parts'].get(name)
        if left is None or right is None:
            diffs.append({'part':name,'kind':'missing_part','in_reference':left is not None,'in_candidate':right is not None})
            same_structure=False;same_content=False;continue
        if 'tree' in left or 'tree' in right:
            if left.get('tree')!=right.get('tree'):
                same_structure=False;diffs.append({'part':name,'kind':'xml_structure_or_format'})
            if left.get('story')!=right.get('story'):
                same_content=False;diffs.append({'part':name,'kind':'document_content'})
            elif left.get('tree') and right.get('tree') and (left['tree'][1] is not None or right['tree'][1] is not None) and left['tree'][1]!=right['tree'][1]:
                # Unresolved entity declarations may change rendered text even
                # when the extracted story strings are identical.
                same_content=False;diffs.append({'part':name,'kind':'document_content_unverified_dtd'})
        elif left['sha256']!=right['sha256']:
            same_structure=False;same_content=False
            difference={'part':name,'kind':'binary_content'}
            if inspect_embedded and (left.get('embedded_docx') or right.get('embedded_docx')):
                try:
                    with zipfile.ZipFile(io.BytesIO(reference)) as left_zip,zipfile.ZipFile(io.BytesIO(candidate)) as right_zip:
                        difference['embedded_docx']=compare_docx(left_zip.read(name),right_zip.read(name),inspect_embedded=False)
                except Exception as error:
                    difference['embedded_docx']={'equivalent':False,'error':{'type':type(error).__name__,'message':str(error)}}
            diffs.append(difference)
    valid=not a['errors'] and not b['errors']
    return {'equivalent':valid and same_content and same_structure,
            'content_equal':valid and same_content,'structure_and_format_equal':valid and same_structure,
            'archive_bytes_equal':reference == candidate,
            'reference_sha256':a['sha256'],'candidate_sha256':b['sha256'],
            'reference_errors':a['errors'],'candidate_errors':b['errors'],
            'reference_size_bytes':a['size_bytes'],'candidate_size_bytes':b['size_bytes'],
            'size_ratio':b['size_bytes']/a['size_bytes'] if a['size_bytes'] else None,
            'reference_uncompressed_bytes':a['uncompressed_bytes'],
            'candidate_uncompressed_bytes':b['uncompressed_bytes'],'differences':diffs}

