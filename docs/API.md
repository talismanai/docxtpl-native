# Python API contract

`docxtpl_native` is the Python interface to the Rust engine. Its classes are
its own: they do not inherit from, import or wrap the classes of the
[docxtpl](https://github.com/elapouya/python-docx-template) library, and the
package does not need docxtpl to be installed. docxtpl is used only by the
differential laboratory (`paritylab`) and the tests that compare both
libraries.

The API keeps the names, signatures and observable behaviour of the docxtpl
uses listed below. Where it differs, the difference is intentional and listed
under [Divergences](#divergences). What "the same behaviour" means, and how it is measured, is in
[CONTRACT.md](CONTRACT.md).

## Installation

```bash
pip install docxtpl-native              # templates, rich text, images, listings
pip install 'docxtpl-native[subdoc]'    # adds file and programmatic subdocuments
```

The minimal installation requires python-docx, Jinja2 and lxml. The `subdoc`
extra adds docxcompose, as `docxtpl[subdoc]` does. Neither installs docxtpl.

## Classes

All classes are plain Python classes (metaclass `type`) and can be
subclassed. A subclass is recognised wherever its base class is.

### `DocxTemplate(template_file)`

`template_file` is a path, a path-like object or a binary file object.

| Member | Behaviour |
| --- | --- |
| `render(context, jinja_env=None, autoescape=False)` | Renders body, headers, footers, footnotes and core properties in the native VM |
| `save(filename)` | Writes the document to a path or a writable binary file object |
| `get_docx()`, `init_docx(reload=True)`, `docx` | The python-docx `Document` of the template, loaded on demand |
| `new_subdoc(docpath=None)` | Returns a `Subdoc` |
| `build_url_id(url)` | Adds an external hyperlink relationship and returns its id |
| `get_undeclared_template_variables(jinja_env=None, context=None)` | Names used by the template and not defined by it |
| `replace_media`, `replace_pic`, `replace_embedded`, `replace_zipname`, `reset_replacements` | Media and part replacement applied on save |
| `get_file_crc(source)` | Static; CRC-32 of a file as the replacement maps key it |
| `get_xml`, `write_xml`, `patch_xml`, `resolve_listing`, `build_xml`, `render_xml_part`, `render_properties`, `render_footnotes`, `get_headers_footers`, `get_headers_footers_encoding`, `build_headers_footers_xml`, `map_headers_footers_xml`, `map_tree`, `fix_tables`, `fix_docpr_ids`, `get_part_xml`, `get_pic_map`, `pre_processing`, `post_processing`, `render_init`, `xml_to_string` | The semi-public steps of a render, with docxtpl's signatures |
| `template_file`, `is_rendered`, `is_saved`, `allow_missing_pics`, `crc_to_new_media`, `crc_to_new_embedded`, `zipname_to_replace`, `pics_to_replace`, `HEADER_URI`, `FOOTER_URI` | Attributes with docxtpl's meaning |
| Other attributes | Delegated to the loaded python-docx `Document` |
| `last_execution` | Extension: statistics of the last render |
| `open_package()`, `commit_package(package)` | Extension: the document's package, read and changed natively without python-docx (below) |

#### Native package

`open_package()` returns a `NativePackage`: a copy of the document's package,
the rendered document once rendered and the template before. It does not
build the python-docx model.

| `NativePackage` member | Behaviour |
| --- | --- |
| `names()`, `name in package` | Member names in archive order, and membership |
| `read(name)` | A member's bytes; `KeyError` if missing |
| `write(name, data)` | Replaces or adds a member |
| `relationships(part)` | The part's relationships as `(id, type, target, external)` |
| `relate(part, type, target, external=False)` | Adds a relationship and returns its id |
| `add_image(data, extension, content_type)` | Adds an image under `word/media` (reusing an identical one), registers its content type and returns its name |
| `to_bytes()` | The package as a ZIP archive; unchanged members keep their compression |

`commit_package(package)` makes the document those bytes: the rendered
document once rendered, so `save` writes it and replacements registered with
`replace_media` and the others still apply, or the template that `render`
reads next. Saving with replacements walks relationships, as python-docx
does, so only parts reachable through relationships are guaranteed to be
written.

There is one owner at a time. While the python-docx model is exposed
(`docx`, `get_docx()`, or a render that exposes it), `open_package` and
`commit_package` raise `UnsupportedFeature`. After a commit, `docx` builds the
model from the committed bytes. Neither works during a render.

### `RichText(text=None, **text_prop)` and `R`

`add(text, style=None, color=None, highlight=None, size=None, subscript=None,
superscript=None, bold=False, italic=False, underline=False, strike=False,
font=None, url_id=None, rtl=False, lang=None)` appends a run. Adding a
`RichText` appends its XML. `xml` holds the WordprocessingML;
`str()`, `__html__()` and `__unicode__()` return it.

### `RichTextParagraph(text=None, **text_prop)` and `RP`

`add(text, parastyle=None)` appends a paragraph. Text that is not a
`RichText` is wrapped in one first.

### `Listing(text)`

Escapes `text` and keeps `\n`, `\a`, `\t` and `\f` as line, paragraph, tab and
page breaks in the template's own style.

### `InlineImage(tpl, image_descriptor, width=None, height=None, anchor=None, title=None, descr=None)`

`image_descriptor` is a path or a binary file object. `width` and `height` are
python-docx lengths. `anchor` adds a hyperlink to the picture. `title` and
`descr` are converted with `str()`. The class attributes default to `None`, as
in docxtpl, so a subclass that skips `__init__` still reads them.

### `Subdoc(tpl, docpath=None)`

Without `docpath`, a programmatic document: attribute access is delegated to
a new python-docx `Document` (`add_paragraph`, `add_table`, ...). With
`docpath`, the document at that path or file object is inserted, with its
styles, numbering, media and relationships. Needs the `subdoc` extra.

### Other names

- `UnsupportedFeature` (a `NotImplementedError`): a feature the engine does
  not implement yet. It is never answered by falling back to docxtpl.
- `compose(documents)`: concatenates documents and returns DOCX bytes.
- `capabilities()`: reports whether the native extension is loaded.
  `reference_fallback` is always `False`.
- `python -m docxtpl_native [-h] [-o] [-q] template_path json_path output_filename`:
  the command-line interface of `python -m docxtpl`, with the same arguments,
  checks and messages.

### Jinja2 environment options

A `jinja_env` passed to `render` may change the block, variable and comment
delimiters. It may not set `line_statement_prefix`, `line_comment_prefix` or
`newline_sequence`, nor a bytecode cache: those raise `UnsupportedFeature`.
The native lexer keeps the whitespace around line statements and line
comments differently from Jinja2's, so accepting them would produce a
different document; a bytecode cache stores compiled Python, which the native
compiler neither reads nor writes.

## Divergences

Each divergence is listed under `divergences` in
`compatibility/spec_contracts.json` with the tests that pin it.

| docxtpl | docxtpl_native | Why |
| --- | --- | --- |
| Classes defined by docxtpl | Classes defined here. `isinstance(docxtpl_native.RichText(), docxtpl.RichText)` is `False`, and the reverse too | The product does not depend on docxtpl |
| An `InlineImage` metaclass made docxtpl images count as native images | No metaclass. Objects created with docxtpl classes are ordinary Python objects to the engine; they are not inserted as rich text, images or subdocuments | Same as above. Create values with `docxtpl_native` classes |
| Monkeypatching `docxtpl.inline_image` or `docxtpl.subdoc` globals changes rendering | Has no effect. The globals the engine honours are those of `docxtpl_native` and of the libraries it uses (python-docx, docxcompose) | docxtpl is not loaded |
| docxtpl 0.17 compatibility (images without `anchor`, `title`, `descr`) | Not provided | The supported reference is docxtpl 0.20.2 |
| `python -m docxtpl` | `python -m docxtpl_native`; the usage line names this module | Own entry point |

Document output does not change: the same template and context produce the
same document as docxtpl 0.20.2, except for three deliberate differences,
listed with their reasons in
[STATUS.md](STATUS.md#deliberate-differences-from-docxtpl).

### Interpreter internals

Templates run in a native VM, not as Python code generated by Jinja2. Code that
inspects Jinja2's generated Python or the CPython interpreter observes
differences, listed as `jinja.internal.*` divergences in the catalog and as
[accepted debts](STATUS.md#accepted-debts):

| Jinja2 | docxtpl_native |
| --- | --- |
| An async template is a stack of Python generators; a legacy three-argument `throw()` warns once per layer, and async-generator hooks see each one | One native coroutine: one warning, no template async generators |
| A filter returning an invalid value raises a `SystemError` whose text depends on CPython's call-site specialization | The unspecialized message |
| `indent` hands `join` a Python generator (its frame, code object and reference count are visible) | A native iterator |
| Exact code clones of `markupsafe.escape`, and the code names of generated coroutines | A clone of `escape` is treated as the original; native coroutines have their own code |
| `Template.from_code` receives CPython bytecode; `Environment.cache` stores Python-compiled templates | No bytecode: `from_code` is not called, and the cache protocol is not used (reload rules are followed natively) |
| Python `SyntaxError`s from generated code (a repeated `_loop_vars` keyword) | No generated code, so no such error |
| `tojson`, and `join` with an attribute, run Python bodies whose frames appear in tracebacks | Native bodies, without those frames |
| One less reference to an import target during an import callback; a `Context.vars` mapping sees `get_exported`'s frame | One extra reference; no Python frame |
| An invalid header raises `XMLSyntaxError` with lxml's `error_log` entries | The same message, code and position, with an empty `error_log` |
| A replaced `auto_aiter` imported by the generated template module lives until that module's cycle is collected | Released when the render ends |
| An async loop's `repr` runs `AsyncLoopContext.__repr__`, so the unawaited `length` coroutine's warning points into Jinja2's runtime | The `repr` is built natively; the warning points to the caller |
| Reference counts of replaced async loop factories during their call | One reference more or less |
| An exact code clone of `AsyncLoopContext._to_iterator` installed before `docxtpl_native` loads is a callback | Taken for the original (a clone installed later is a callback) |

### Async renders on another thread

An async render's coroutine can be suspended on one thread and resumed,
thrown into, closed or collected on another, as with Jinja2; callbacks and
`finally` blocks then run on the resuming thread. This holds when the render
is suspended at an await of the template itself (an awaited call or value, an
`async for` step, a deferred block). A render suspended inside a host call
that awaits under its own frame (the async `join` filter, a macro called from
Python code, a Python callback that renders) stays on its thread: operating on
it from another thread raises `UnsupportedFeature`, and collecting it there
reports an unraisable `RuntimeError` and leaks the suspended render.

Including a Jinja2 `Template` object (`{% include tpl %}` with a template from
another environment) renders it through Jinja2, with or without the caller's
context as Jinja2 does. In an async render this raises `UnsupportedFeature`.

### Templates parsed without Jinja2's parser

The engine always compiles and renders templates natively. Jinja2's parser
still validates a template, so syntax errors are exactly Jinja2's, except on
the qualified path below, where the native parser is used alone because both
parsers read the template the same way and nothing in it can run code at
compile time.

The qualified path applies when `DocxTemplate.render` uses the default
environment, or an explicit `jinja2.Environment` that adds nothing to the
defaults: exact class, synchronous, default filters, tests, globals and
policies, default delimiters, no extensions, loader, bytecode cache,
`finalize`, replaced method or custom undefined class. The context must be a
`dict` with `str` keys. If it holds only data (`str`, numbers, `bool`,
`None`, `bytes`, lists, tuples, dicts, and the package's `RichText`,
`RichTextParagraph` and `Listing`), every part of the document qualifies.
If it holds other Python objects, only the body does; the parts rendered
after it go through Jinja2's parser, because the objects may have run code
in the meantime. Any callback other than Jinja2's own filters and tests, or
formatting a value that is not plain data, also sends the following parts
through Jinja2's parser.

A qualified template uses only:

- text, and outputs of the values below (no comments, no `-`/`+`
  whitespace control);
- variable paths: context names, `.attribute`, `['key']` with printable ASCII
  keys, and `[index]` with small decimal indices;
- literals: printable ASCII strings without escapes, quotes, `<`, `>` or
  `&`; decimal integers up to 9 digits without a leading zero; `true`,
  `false`, `none` in either case;
- the filters `upper`, `lower`, `title`, `capitalize`, `trim`, `default`/`d`,
  `length`, `count`, `join`, `first`, `last`, `string`, `e` and `escape`, with
  positional literal arguments;
- `+`, `-`, `*`, `/`, `//`, `%` and `~` between values (not nested), and
  `a if condition else b`;
- calls of a path with positional literal or value arguments;
- `if`/`elif`/`else` with `not`, `and`, `or`, comparisons (`==`, `!=`, `<`,
  `<=`, `>`, `>=`, `in`, `not in`) and the `defined`, `undefined` and `none`
  tests;
- `for` over a value with a name or tuple target, an optional `else`, and the
  `loop` attributes `index`, `index0`, `revindex`, `revindex0`, `first`,
  `last`, `length`, `depth`, `depth0`, `previtem` and `nextitem`;
- `set` of one name, and macros with name parameters and literal defaults,
  called after they are defined.

Everything else (blocks, `extends`, `include`, `import`, `call` blocks,
`caller`, `varargs`, `kwargs`, `namespace` assignments, filters and tests not
listed, other literals, extensions, custom delimiters, environments with
hooks) is supported through Jinja2's parser.
