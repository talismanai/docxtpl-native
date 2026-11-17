//! Subdoc support: merge a sub-document's body into the master document,
//! remapping relationships (images, hyperlinks, recursive parts), styles
//! (with conflict renaming), numbering and footnotes.

use crate::image::ImageInfo;
use crate::package::{rel_type, resolve_target, Package};
use crate::patch::sub as psub;
use crate::template::{TplCore, DOCUMENT_PART};
use crate::xmldom::{Document, Element, Node};
use std::collections::{HashMap, HashSet};

const STYLES_CT: &str =
    "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml";
const NUMBERING_CT: &str =
    "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml";
const FOOTNOTES_CT: &str =
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml";
const ENDNOTE_CT: &str =
    "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml";

/// Options for the subdoc merge pipeline. [`SubdocOpts::new`] reproduces the
/// default render-pipeline behavior (restart numbering, dissolve property
/// fields, rename style conflicts).
#[derive(Debug, Clone, Copy)]
pub struct SubdocOpts {
    /// preserve the subdoc's non-empty body-level sectPr as its own section
    pub keep_sections: bool,
    /// restart numbering of the subdoc's first list (docxcompose
    /// restart_numbering, default true)
    pub restart_numbering: bool,
    /// dissolve DOCPROPERTY fields keeping their cached values; the Composer
    /// does its own value-aware dissolve and disables this
    pub dissolve_property_fields: bool,
    /// retarget style references to the master's same-named style instead of
    /// renaming conflicting definitions (docxcompose preserve_styles=False)
    pub map_styles_by_name: bool,
    /// copy the subdoc's default paragraph style properties onto unstyled
    /// runs/paragraphs (docxcompose preserve_styles=True)
    pub retain_default_formatting: bool,
    /// docxcompose preserve_styles=True style merge: conflicting styles are
    /// copied with `increment_name`-suffixed ids/names and equivalent styles
    /// (ignoring w:name/w:rsid) are reused instead of copied, tracked across
    /// merges via `TplCore::preserved_styles`. false keeps the legacy `X_1`
    /// renaming used by the render pipeline.
    pub preserve_styles: bool,
}

impl SubdocOpts {
    pub fn new(keep_sections: bool) -> SubdocOpts {
        SubdocOpts {
            keep_sections,
            restart_numbering: true,
            dissolve_property_fields: true,
            map_styles_by_name: false,
            retain_default_formatting: false,
            preserve_styles: false,
        }
    }
}

/// Produce the block-level xml for a subdocument to be inserted into the
/// master. With `keep_sections` the subdoc's non-empty body-level `w:sectPr`
/// is preserved: the subdoc content becomes its own section (page
/// size/orientation/margins and header/footer references kept).
pub fn subdoc_xml_opts(
    tpl: &mut TplCore,
    subdoc_bytes: &[u8],
    keep_sections: bool,
) -> Result<String, String> {
    Ok(subdoc_xml_info(tpl, subdoc_bytes, keep_sections)?.0)
}

/// Full subdoc merge; returns the body fragment xml and whether a section
/// break was preserved (callers may use it to skip their own page break).
pub(crate) fn subdoc_xml_info(
    tpl: &mut TplCore,
    subdoc_bytes: &[u8],
    keep_sections: bool,
) -> Result<(String, bool), String> {
    let mut options = SubdocOpts::new(keep_sections);
    // SubdocComposer maps language-dependent style IDs through their names;
    // same-named styles already in the master retain the master's definition.
    options.map_styles_by_name = tpl.python_source_contract;
    subdoc_xml_info_opts(tpl, subdoc_bytes, options)
}

/// Full subdoc merge with explicit options (see [`SubdocOpts`]).
pub(crate) fn subdoc_xml_info_opts(
    tpl: &mut TplCore,
    subdoc_bytes: &[u8],
    opts: SubdocOpts,
) -> Result<(String, bool), String> {
    let keep_sections = opts.keep_sections;
    let sub_pkg = Package::from_bytes(subdoc_bytes)?;
    let doc_xml = sub_pkg
        .get_string(DOCUMENT_PART)
        .ok_or_else(|| "subdoc has no word/document.xml".to_string())?;
    // python-docx loads Word XML with blank structural nodes removed before
    // SubdocComposer copies and later serializes the detached body.
    let doc_xml = if tpl.python_source_contract {
        match tpl.header_footer_normalizer {
            Some(normalize) => normalize(&doc_xml)?,
            None => doc_xml,
        }
    } else {
        doc_xml
    };

    // extract body children; stash a non-empty body-level sectPr when
    // keep_sections is set, drop it otherwise (docxcompose parity)
    let dom = Document::parse(&doc_xml)?;
    let body = dom
        .root
        .find("w:body")
        .ok_or_else(|| "subdoc has no w:body".to_string())?;
    let mut body_children: Vec<Node> = Vec::new();
    let mut sectpr_xml: Option<String> = None;
    for c in &body.children {
        if let Node::Elem(e) = c {
            if e.name == "w:sectPr" {
                if keep_sections
                    && e.children.iter().any(|ch| matches!(ch, Node::Elem(_)))
                {
                    let mut s = String::new();
                    e.serialize(&mut s);
                    sectpr_xml = Some(s); // last one wins
                }
                continue;
            }
        }
        body_children.push(c.clone());
    }
    let mut body_xml = String::new();
    for c in &body_children {
        match c {
            Node::Elem(e) => e.serialize(&mut body_xml),
            Node::Text(t) => body_xml.push_str(t),
        }
    }

    // the subdoc content becomes its own section: append its sectPr as a
    // paragraph-level sectPr (before remap_rids so header/footer references
    // participate in the rId remap)
    if let Some(sp) = &sectpr_xml {
        body_xml.push_str("<w:p><w:pPr>");
        body_xml.push_str(sp);
        body_xml.push_str("</w:pPr></w:p>");
    }

    // dissolve custom property fields (keep the values)
    if opts.dissolve_property_fields {
        body_xml = dissolve_property_fields(body_xml);
    }

    // preserve_styles=True: retain the subdoc's default paragraph style
    // formatting on unstyled runs/paragraphs (before the style merge, while
    // references still use the subdoc's style ids)
    if opts.retain_default_formatting {
        body_xml = retain_formatting_from_default_styles(tpl, &sub_pkg, body_xml)?;
    }

    let mut rid_map: HashMap<String, String> = HashMap::new();
    // header/footer parts copied for the preserved sections (master part
    // names); their styles/numbering references are merged together with
    // the body's below
    let mut hf_parts: Vec<String> = Vec::new();

    // remap relationships of the sub document part (recursively for parts)
    let sub_rels = sub_pkg.rels(DOCUMENT_PART);
    for rel in &sub_rels.rels {
        let new_rid = if rel.rel_type == rel_type::IMAGE {
            let target_part = resolve_target(DOCUMENT_PART, &rel.target);
            let Some(blob) = sub_pkg.get(&target_part) else {
                continue;
            };
            let pkg = tpl.package.as_mut().ok_or("package not loaded")?;
            let (partname, _) = add_image_part(pkg, blob, &target_part);
            let target = crate::package::relative_target(DOCUMENT_PART, &partname);
            pkg.add_rel(DOCUMENT_PART, rel_type::IMAGE, &target, false)
        } else if rel.rel_type == rel_type::HYPERLINK {
            let pkg = tpl.package.as_mut().ok_or("package not loaded")?;
            pkg.add_rel(DOCUMENT_PART, rel_type::HYPERLINK, &rel.target, true)
        } else if rel.rel_type == rel_type::STYLES
            || rel.rel_type == rel_type::NUMBERING
            || rel.rel_type == rel_type::FOOTNOTES
            || rel.rel_type == rel_type::COMMENTS
            || rel.rel_type == rel_type::ENDNOTE
        {
            // handled separately below
            continue;
        } else if keep_sections
            && (rel.rel_type == rel_type::HEADER || rel.rel_type == rel_type::FOOTER)
        {
            // referenced from the preserved sectPr: copy with a fresh part
            // name on collision so the master's own header/footer parts
            // stay untouched
            let target_part = resolve_target(DOCUMENT_PART, &rel.target);
            if !sub_pkg.contains(&target_part) {
                continue;
            }
            let pkg = tpl.package.as_mut().ok_or("package not loaded")?;
            let new_part = copy_part_renamed(pkg, &sub_pkg, &target_part);
            let target = crate::package::relative_target(DOCUMENT_PART, &new_part);
            hf_parts.push(new_part);
            pkg.add_rel(DOCUMENT_PART, &rel.rel_type, &target, false)
        } else if !rel.is_external {
            // recursively copy the target part and its own relationships
            let target_part = resolve_target(DOCUMENT_PART, &rel.target);
            if !sub_pkg.contains(&target_part) {
                continue;
            }
            let pkg = tpl.package.as_mut().ok_or("package not loaded")?;
            copy_part_recursive(pkg, &sub_pkg, &target_part, &target_part, 0);
            let target = crate::package::relative_target(DOCUMENT_PART, &target_part);
            pkg.add_rel(DOCUMENT_PART, &rel.rel_type, &target, false)
        } else {
            let pkg = tpl.package.as_mut().ok_or("package not loaded")?;
            pkg.add_rel(DOCUMENT_PART, &rel.rel_type, &rel.target, true)
        };
        rid_map.insert(rel.id.clone(), new_rid);
    }

    // apply rId remapping in the body xml (single pass over the xml,
    // independent of how many ids were remapped)
    body_xml = remap_rids(&body_xml, &rid_map);

    // resume-section break: clone the master's current body sectPr so the
    // content before the subdoc keeps the master's page setup. Inserted
    // AFTER remap_rids: its r:ids are the master's own and must not be
    // touched by the subdoc's rid_map.
    if sectpr_xml.is_some() {
        if let Some(master_sp) = master_resume_sectpr(tpl) {
            body_xml = format!("<w:p><w:pPr>{}</w:pPr></w:p>{}", master_sp, body_xml);
        }
    }

    // renumber bookmarks so ids don't collide with the master document
    body_xml = renumber_bookmarks(tpl, body_xml);

    // prepare the footnotes/comments contents early: their image/external
    // rids are remapped here; the style/numbering references inside them are
    // merged jointly with the body below, so a style/numId referenced from
    // the body and from a footnote/comment maps to the same new id
    let mut fn_content = prepare_note_part_content(tpl, &sub_pkg, "word/footnotes.xml")?;
    let mut en_content = prepare_note_part_content(tpl, &sub_pkg, "word/endnotes.xml")?;
    let mut c_content = if body_xml.contains("w:commentReference") {
        prepare_note_part_content(tpl, &sub_pkg, "word/comments.xml")?
    } else {
        None
    };

    // styles + numbering merge. The copied header/footer parts and the
    // footnotes/comments contents may reference the subdoc's
    // styles/numbering too: merge body and those contents as ONE string so a
    // style/numId referenced from several places maps to the same new id,
    // then split back and write the hf parts out.
    let style_renames;
    let skip_existing;
    let extras = hf_parts.len()
        + fn_content.is_some() as usize
        + en_content.is_some() as usize
        + c_content.is_some() as usize;
    if extras == 0 {
        skip_existing = (tpl.python_source_contract || tpl.python_subdoc_numbering_contract)
            && only_existing_unchanged_styles(tpl, &sub_pkg, &body_xml);
        style_renames = merge_styles(tpl, &sub_pkg, &mut body_xml, opts)?;
        if !skip_existing { body_xml = merge_numbering(tpl, &sub_pkg, body_xml, &style_renames, true)?; }
    } else {
        let hf_contents: Vec<String> = {
            let pkg = tpl.package.as_mut().ok_or("package not loaded")?;
            hf_parts
                .iter()
                .map(|p| pkg.get_string(p).unwrap_or_default())
                .collect()
        };
        let mut joined = body_xml;
        for c in &hf_contents {
            joined.push_str(HF_PART_SEP);
            joined.push_str(c);
        }
        if let Some(c) = &fn_content {
            joined.push_str(HF_PART_SEP);
            joined.push_str(c);
        }
        if let Some(c) = &en_content {
            joined.push_str(HF_PART_SEP);
            joined.push_str(c);
        }
        if let Some(c) = &c_content {
            joined.push_str(HF_PART_SEP);
            joined.push_str(c);
        }
        skip_existing = (tpl.python_source_contract || tpl.python_subdoc_numbering_contract)
            && only_existing_unchanged_styles(tpl, &sub_pkg, &joined);
        style_renames = merge_styles(tpl, &sub_pkg, &mut joined, opts)?;
        if !skip_existing { joined = merge_numbering(tpl, &sub_pkg, joined, &style_renames, false)?; }
        let mut segs = joined.split(HF_PART_SEP);
        body_xml = segs.next().unwrap_or_default().to_string();
        {
            let pkg = tpl.package.as_mut().ok_or("package not loaded")?;
            for (part, seg) in hf_parts.iter().zip(segs.by_ref()) {
                let enc = pkg.encoding_of(part);
                pkg.set(
                    part,
                    crate::package::encode_part_owned(seg.to_string(), &enc),
                );
            }
        }
        if fn_content.is_some() {
            fn_content = Some(segs.next().unwrap_or_default().to_string());
        }
        if en_content.is_some() {
            en_content = Some(segs.next().unwrap_or_default().to_string());
        }
        if c_content.is_some() {
            c_content = Some(segs.next().unwrap_or_default().to_string());
        }
        for part in &hf_parts {
            tpl.invalidate_part(part);
        }
    }

    // restart numbering of the first list in the subdoc (docxcompose default)
    if opts.restart_numbering {
        body_xml = restart_first_numbering(tpl, body_xml, skip_existing)?;
    }

    // footnotes merge
    body_xml = merge_footnotes(tpl, body_xml, fn_content)?;

    // endnotes merge
    body_xml = merge_endnotes(tpl, body_xml, en_content)?;

    // comments merge (+ w15 commentsExtended / w16cid commentsIds)
    let (body_xml, comments_merged) = merge_comments(tpl, body_xml, c_content)?;
    if comments_merged {
        merge_comments_extended(tpl, &sub_pkg);
    }

    Ok((body_xml, sectpr_xml.is_some()))
}

/// Separator between the body, the copied header/footer contents and the
/// footnotes/comments contents during the joint styles/numbering merge
/// (never present in real xml).
const HF_PART_SEP: &str = "\u{1}DTPLHF\u{1}";

/// Serialize the master document's body-level sectPr for the resume-section
/// break; None when it is missing/empty, or when the last block before it
/// already ends a section (paragraph-level sectPr — e.g. from a previous
/// keep_sections merge), where a resume break would create an empty section
/// (a blank page).
fn master_resume_sectpr(tpl: &mut TplCore) -> Option<String> {
    let pkg = tpl.package.as_ref()?;
    let xml = pkg.get_string(DOCUMENT_PART)?;
    let dom = Document::parse(&xml).ok()?;
    let body = dom.root.find("w:body")?;
    let mut sectpr: Option<&Element> = None;
    let mut last_block: Option<&Element> = None;
    for c in &body.children {
        if let Node::Elem(e) = c {
            if e.name == "w:sectPr" {
                sectpr = Some(e);
            } else {
                last_block = Some(e);
            }
        }
    }
    if let Some(p) = last_block {
        if p.name == "w:p"
            && p.find("w:pPr")
                .and_then(|ppr| ppr.find("w:sectPr"))
                .is_some()
        {
            return None;
        }
    }
    let e = sectpr?;
    if !e.children.iter().any(|ch| matches!(ch, Node::Elem(_))) {
        return None;
    }
    let mut s = String::new();
    e.serialize(&mut s);
    Some(s)
}

/// Copy a part into the master, allocating a fresh part name on collision
/// (word/header1.xml -> word/header2.xml). Returns the master part name.
fn copy_part_renamed(master: &mut Package, sub: &Package, part: &str) -> String {
    if !master.contains(part) {
        copy_part_recursive(master, sub, part, part, 0);
        return part.to_string();
    }
    let (dir, file) = match part.rfind('/') {
        Some(i) => (&part[..i + 1], &part[i + 1..]),
        None => ("", part),
    };
    let (stem, ext) = match file.find('.') {
        Some(i) => (&file[..i], &file[i..]),
        None => (file, ""),
    };
    let base = stem.trim_end_matches(|c: char| c.is_ascii_digit());
    for n in 1..1000 {
        let cand = format!("{}{}{}{}", dir, base, n, ext);
        if !master.contains(&cand) {
            copy_part_recursive(master, sub, part, &cand, 0);
            return cand;
        }
    }
    part.to_string() // unreachable in practice
}

/// Apply an rId remap with a single regex pass over the xml (previously one
/// full scan per remapped id). Attribute values not present in the map are
/// left untouched, which also preserves the old `old != new` skip.
fn remap_rids(xml: &str, map: &HashMap<String, String>) -> String {
    if map.is_empty() {
        return xml.to_string();
    }
    psub(
        r#"((?:r|o):(?:embed|id|link|pict|dm|lo|qs|cs|relid|rel)=")([^"]+)""#,
        |m| {
            let old = m.get(2).unwrap().as_str();
            match map.get(old) {
                Some(new) if new != old => format!("{}{}\"", m.get(1).unwrap().as_str(), new),
                _ => m.get(0).unwrap().as_str().to_string(),
            }
        },
        xml,
    )
}

/// Wrap a body fragment in a root element for DOM parsing.
pub(crate) fn parse_body_fragment(body_xml: &str) -> Result<Document, String> {
    Document::parse(&format!(
        "<w:body xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\" xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">{}</w:body>",
        body_xml
    ))
}

fn serialize_body_fragment(dom: &Document) -> String {
    let mut out = String::new();
    for c in &dom.root.children {
        match c {
            Node::Elem(e) => e.serialize(&mut out),
            Node::Text(t) => out.push_str(t),
        }
    }
    out
}

/// Restart the numbering of the first list of the subdoc (docxcompose's
/// restart_first_numbering, enabled by default): the first numbered
/// paragraph (per style) gets a new <w:num> with startOverride=1 so the
/// inserted list restarts instead of continuing the master's sequence.
fn restart_first_numbering(tpl: &mut TplCore, body_xml: String, existing_style_contract: bool) -> Result<String, String> {
    let typed_contract = tpl.python_subdoc_numbering_contract || existing_style_contract;
    let pkg = tpl.package.as_mut().ok_or("package not loaded")?;
    if !pkg.contains("word/numbering.xml") {
        return Ok(body_xml);
    }
    let mut dom = match parse_body_fragment(&body_xml) {
        Ok(d) => d,
        Err(_) => return Ok(body_xml),
    };

    let styles_xml = pkg.get_string("word/styles.xml").unwrap_or_default();
    let styles_dom = Document::parse(&styles_xml).ok();

    // style lookup table, built once (was: re-collected and deep-cloned
    // from the styles part for every paragraph)
    let mut style_elems: Vec<&Element> = Vec::new();
    if let Some(d) = styles_dom.as_ref() {
        collect_style_elems(&d.root, &mut style_elems);
    }
    let mut style_by_id: HashMap<String, &Element> = HashMap::new();
    for e in style_elems {
        if let Some(id) = e.get_attr("w:styleId") {
            style_by_id.entry(id.to_string()).or_insert(e);
        }
    }

    let mut restarted: HashSet<String> = HashSet::new();
    let mut new_nums: Vec<Element> = Vec::new();
    // paragraphs to retarget: (paragraph index in body, new numId)
    let mut retarget: Vec<(usize, i64)> = Vec::new();

    // next free numId in master numbering (after the merge)
    let numbering_xml = pkg.get_string("word/numbering.xml").unwrap_or_default();
    let numbering_dom = match Document::parse(&numbering_xml) {
        Ok(d) => d,
        Err(_) => return Ok(body_xml),
    };
    let mut max_num: i64 = 0;
    let mut max_abs: i64 = -1;
    find_max_numbering(&numbering_dom.root, &mut max_abs, &mut max_num);
    let mut next_num_id = max_num + 1;

    // num / abstractNum lookup tables, built once (first match wins, like
    // the previous per-paragraph linear find)
    let mut num_by_id: HashMap<i64, &Element> = HashMap::new();
    let mut abs_by_id: HashMap<i64, &Element> = HashMap::new();
    {
        let mut nums: Vec<&Element> = Vec::new();
        numbering_dom.root.iter_descendants("w:num", &mut nums);
        for e in nums {
            if let Some(id) = e.get_attr("w:numId").and_then(|v| v.parse::<i64>().ok()) {
                num_by_id.entry(id).or_insert(e);
            }
        }
        let mut abss: Vec<&Element> = Vec::new();
        numbering_dom
            .root
            .iter_descendants("w:abstractNum", &mut abss);
        for e in abss {
            if let Some(id) = e
                .get_attr("w:abstractNumId")
                .and_then(|v| v.parse::<i64>().ok())
            {
                abs_by_id.entry(id).or_insert(e);
            }
        }
    }

    let para_count = dom
        .root
        .children
        .iter()
        .filter(|c| matches!(c, Node::Elem(e) if e.name == "w:p"))
        .count();
    let mut para_idx = 0usize;
    for i in 0..para_count {
        let _ = i;
        // find the para_idx-th w:p
        let Some(p) = nth_direct_para(&dom.root, para_idx) else {
            break;
        };
        para_idx += 1;
        let Some(ppr) = p.find("w:pPr") else { continue };
        let Some(style_id) = ppr
            .find("w:pStyle")
            .and_then(|e| e.get_attr("w:val"))
            .map(|s| s.to_string())
        else {
            continue;
        };
        if restarted.contains(&style_id) {
            continue;
        }
        let Some(style_el) = style_by_id.get(style_id.as_str()) else {
            continue;
        };
        let mut outlines = Vec::new();
        if typed_contract { style_el.iter_descendants("w:outlineLvl", &mut outlines); }
        if style_el.find("w:outlineLvl").is_some() || !outlines.is_empty() {
            continue; // headings are not restarted
        }
        // numId from the paragraph, else from the style
        let num_id = ppr
            .find("w:numPr")
            .and_then(|n| n.find("w:numId"))
            .and_then(|e| e.get_attr("w:val").and_then(|v| v.parse::<i64>().ok()))
            .or_else(|| {
                if typed_contract {
                    let mut ids = Vec::new();
                    style_el.iter_descendants("w:numId", &mut ids);
                    ids.first().and_then(|e| e.get_attr("w:val").and_then(|v| v.parse::<i64>().ok()))
                } else {
                    style_el.find("w:numId")
                        .and_then(|e| e.get_attr("w:val").and_then(|v| v.parse::<i64>().ok()))
                }
            });
        let Some(num_id) = num_id else { continue };

        // locate the num + abstractNum in master numbering
        let Some(num_el) = num_by_id.get(&num_id) else {
            continue;
        };
        let Some(anum_id) = num_el
            .find("w:abstractNumId")
            .and_then(|e| e.get_attr("w:val").and_then(|v| v.parse::<i64>().ok()))
        else {
            continue;
        };
        let Some(anum_el) = abs_by_id.get(&anum_id) else {
            continue;
        };
        // do not restart bullets
        let is_bullet = anum_el.find_all("w:lvl").iter().any(|lvl| {
            lvl.get_attr("w:ilvl") == Some("0")
                && lvl
                    .find("w:numFmt")
                    .and_then(|f| f.get_attr("w:val"))
                    == Some("bullet")
        });
        if is_bullet {
            continue;
        }

        // create the new num with startOverride=1
        let mut new_num = (*num_el).clone();
        new_num.set_attr("w:numId", &next_num_id.to_string());
        let mut lvl_override = Element::new("w:lvlOverride");
        lvl_override.set_attr("w:ilvl", "0");
        let mut start_override = Element::new("w:startOverride");
        start_override.set_attr("w:val", "1");
        lvl_override.children.push(Node::Elem(start_override));
        new_num.children.push(Node::Elem(lvl_override));
        new_nums.push(new_num);
        retarget.push((para_idx - 1, next_num_id));
        next_num_id += 1;
        restarted.insert(style_id);
    }

    if new_nums.is_empty() {
        return Ok(body_xml);
    }

    // apply retargets
    for (pidx, new_id) in retarget {
        if let Some(p) = nth_direct_para_mut(&mut dom.root, pidx) {
            if let Some(ppr) = p.find_mut("w:pPr") {
                if let Some(numpr) = ppr.find_mut("w:numPr") {
                    if let Some(numid) = numpr.find_mut("w:numId") {
                        numid.set_attr("w:val", &new_id.to_string());
                    }
                } else {
                    // insert a numPr after pStyle
                    let mut numpr = Element::new("w:numPr");
                    let mut ilvl = Element::new("w:ilvl");
                    ilvl.set_attr("w:val", "0");
                    let mut numid = Element::new("w:numId");
                    numid.set_attr("w:val", &new_id.to_string());
                    numpr.children.push(Node::Elem(ilvl));
                    numpr.children.push(Node::Elem(numid));
                    let pos = if typed_contract { ppr.children.len() } else { ppr
                        .children
                        .iter()
                        .position(|c| matches!(c, Node::Elem(e) if e.name == "w:pStyle"))
                        .map(|i| i + 1)
                        .unwrap_or(0) };
                    ppr.children.insert(pos, Node::Elem(numpr));
                }
            }
        }
    }

    // append new nums to master numbering (after existing w:num elements)
    let mut numbering_dom = numbering_dom;
    let mut insert_pos = numbering_dom.root.children.len();
    for (i, c) in numbering_dom.root.children.iter().enumerate() {
        if matches!(c, Node::Elem(e) if e.name == "w:num") {
            insert_pos = if typed_contract { i } else { i + 1 };
        }
    }
    for n in new_nums {
        numbering_dom
            .root
            .children
            .insert(insert_pos, Node::Elem(n));
        insert_pos += 1;
    }
    let pkg = tpl.package.as_mut().ok_or("package not loaded")?;
    pkg.set("word/numbering.xml", numbering_dom.serialize().into_bytes());

    Ok(serialize_body_fragment(&dom))
}

fn nth_direct_para(body: &Element, n: usize) -> Option<&Element> {
    body.children
        .iter()
        .filter_map(|c| match c {
            Node::Elem(e) if e.name == "w:p" => Some(e),
            _ => None,
        })
        .nth(n)
}

fn nth_direct_para_mut(body: &mut Element, n: usize) -> Option<&mut Element> {
    body.children
        .iter_mut()
        .filter_map(|c| match c {
            Node::Elem(e) if e.name == "w:p" => Some(e),
            _ => None,
        })
        .nth(n)
}

fn add_image_part(pkg: &mut Package, blob: &[u8], target_part: &str) -> (String, String) {
    let (ext, content_type) = match ImageInfo::parse(blob) {
        Ok(info) => (info.default_ext.to_string(), info.content_type.to_string()),
        Err(_) => {
            let ext = target_part.rsplit('.').next().unwrap_or("png").to_string();
            (ext.clone(), guess_content_type(&ext).to_string())
        }
    };
    let partname = pkg.get_or_add_image(blob, &ext, &content_type);
    (partname, content_type)
}

/// Recursively copy a part and its related parts into the master package,
/// remapping relationship ids inside the copied XML.
/// Recursively copy `src` (in the sub package) to `dst` (in the master),
/// remapping its own relationships. Nested parts keep their names (src == dst
/// in the recursion); only the top-level part may be renamed by
/// [`copy_part_renamed`].
fn copy_part_recursive(master: &mut Package, sub: &Package, src: &str, dst: &str, depth: usize) {
    if depth > 8 || master.contains(dst) {
        return;
    }
    let Some(blob) = sub.get(src) else {
        return;
    };

    // remap the part's own relationships first
    let sub_rels = sub.rels(src);
    let is_xml = src.ends_with(".xml") || src.ends_with(".rels");
    if is_xml {
        let mut xml = String::from_utf8_lossy(blob).into_owned();
        if !sub_rels.rels.is_empty() {
            let mut rid_map: HashMap<String, String> = HashMap::new();
            for rel in &sub_rels.rels {
                let new_rid = if rel.rel_type == rel_type::IMAGE {
                    let target = resolve_target(src, &rel.target);
                    let Some(img) = sub.get(&target) else { continue };
                    let (partname, _) = add_image_part(master, img, &target);
                    let rel_target = crate::package::relative_target(dst, &partname);
                    master.add_rel(dst, rel_type::IMAGE, &rel_target, false)
                } else if rel.is_external {
                    master.add_rel(dst, &rel.rel_type, &rel.target, true)
                } else {
                    let target = resolve_target(src, &rel.target);
                    if sub.contains(&target) {
                        copy_part_recursive(master, sub, &target, &target, depth + 1);
                    }
                    let rel_target = crate::package::relative_target(dst, &target);
                    master.add_rel(dst, &rel.rel_type, &rel_target, false)
                };
                rid_map.insert(rel.id.clone(), new_rid);
            }
            xml = remap_rids(&xml, &rid_map);
        }
        master.set(dst, xml.into_bytes());
    } else {
        // binary parts (embeddings, activeX, …): copy verbatim — a lossy
        // UTF-8 round-trip would replace invalid bytes with U+FFFD and
        // silently corrupt the part
        master.set(dst, blob.to_vec());
    }
    if let Some(ct) = sub_content_type_override(sub, src) {
        master.ensure_content_type_override(dst, &ct);
    }
    // copy the rels file itself if the part had no rels to remap but a rels file exists
    if sub_rels.rels.is_empty() {
        let src_rels = crate::package::rels_path_for(src);
        if let Some(b) = sub.get(&src_rels) {
            let dst_rels = crate::package::rels_path_for(dst);
            if !master.contains(&dst_rels) {
                master.set(&dst_rels, b.to_vec());
            }
        }
    }
}

/// Dissolve DOCPROPERTY fields, keeping the displayed values.
fn dissolve_property_fields(mut xml: String) -> String {
    // simple fields: <w:fldSimple w:instr=" DOCPROPERTY name \* ...">content</w:fldSimple>
    xml = psub(
        r#"(?s)<w:fldSimple [^>]*w:instr="[^"]*DOCPROPERTY[^"]*"[^>]*>(.*?)</w:fldSimple>"#,
        |m| m.get(1).unwrap().as_str().to_string(),
        &xml,
    );
    // complex fields: begin .. separate .. end sequences containing DOCPROPERTY
    xml = psub(
        r#"(?s)(?:<w:r[ >].*?<w:fldChar w:fldCharType="begin"/>.*?</w:r>)(?:(?!<w:fldChar w:fldCharType="separate"/>).)*?DOCPROPERTY.*?<w:r[ >].*?<w:fldChar w:fldCharType="separate"/>.*?</w:r>(.*?)<w:r[ >].*?<w:fldChar w:fldCharType="end"/>.*?</w:r>"#,
        |m| m.get(1).unwrap().as_str().to_string(),
        &xml,
    );
    xml
}

/// Renumber w:bookmarkStart/w:bookmarkEnd ids past the master's max id.
fn renumber_bookmarks(tpl: &mut TplCore, mut body_xml: String) -> String {
    // SubdocComposer renumbers the master at attachment time. Its detached
    // subdocument body is inserted later without rewriting bookmark IDs.
    if tpl.python_source_contract {
        return body_xml;
    }
    let id_re = crate::patch::re(r#"<w:bookmarkStart[^>]* w:id="(\d+)""#);
    // cheap reject before touching the master at all (most subdoc bodies
    // have no bookmarks)
    if !id_re.find(&body_xml).ok().flatten().is_some() {
        return body_xml;
    }
    // the master max id is scanned once, then advanced locally as each
    // subdoc's bookmarks are renumbered (they land in the master in order)
    let base = match tpl.bookmark_next_id {
        Some(b) => b,
        None => {
            let _ = tpl.flush_doc();
            let master_xml = tpl
                .package
                .as_ref()
                .and_then(|p| p.get_string(DOCUMENT_PART))
                .unwrap_or_default();
            let mut max_id: i64 = 0;
            for cap in id_re.captures_iter(&master_xml).flatten() {
                if let Ok(n) = cap[1].parse::<i64>() {
                    max_id = max_id.max(n);
                }
            }
            max_id + 1
        }
    };
    let mut max_assigned = base;
    body_xml = psub(
        r#"(<w:bookmark(?:Start|End)[^>]* w:id=")(\d+)(")"#,
        |m: &fancy_regex::Captures| {
            let n: i64 = m.get(2).unwrap().as_str().parse().unwrap_or(0);
            let new = n + base;
            max_assigned = max_assigned.max(new);
            format!(
                "{}{}{}",
                m.get(1).unwrap().as_str(),
                new,
                m.get(3).unwrap().as_str()
            )
        },
        &body_xml,
    );
    tpl.bookmark_next_id = Some(max_assigned + 1);
    body_xml
}

fn guess_content_type(ext: &str) -> &'static str {
    match ext.to_ascii_lowercase().as_str() {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "bmp" => "image/bmp",
        "tif" | "tiff" => "image/tiff",
        _ => "application/octet-stream",
    }
}

fn sub_content_type_override(pkg: &Package, part_name: &str) -> Option<String> {
    let xml = pkg.get_string("[Content_Types].xml")?;
    let pat = format!("PartName=\"/{}\"", part_name);
    let pos = xml.find(&pat)?;
    let after = &xml[pos..];
    let ct_start = after.find("ContentType=\"")? + "ContentType=\"".len();
    let rest = &after[ct_start..];
    let ct_end = rest.find('"')?;
    Some(rest[..ct_end].to_string())
}

// ---------------- styles ----------------

/// Merge styles referenced by the subdoc body. Conflicting style definitions
/// are renamed and references updated — unless `opts.map_styles_by_name` is
/// set (docxcompose preserve_styles=False): then references to a style whose
/// name exists in the master are retargeted to the master's style id and the
/// subdoc's definition is not copied. With `opts.preserve_styles` (docxcompose
/// preserve_styles=True) the rename uses `increment_name` suffixes for both
/// the style id and its w:name, and an equivalent style already in the master
/// (ignoring w:name/w:rsid; previously preserved copies included) is reused
/// instead of copying. Without it, the legacy `X_1` renaming of the render
/// pipeline is kept. Returns the map of renamed style ids (old -> new).
fn merge_styles(
    tpl: &mut TplCore,
    sub: &Package,
    body_xml: &mut String,
    opts: SubdocOpts,
) -> Result<HashMap<String, String>, String> {
    let map_by_name = opts.map_styles_by_name;
    let mut renames: HashMap<String, String> = HashMap::new();
    let Some(sub_styles_xml) = sub.get_string("word/styles.xml") else {
        return Ok(renames);
    };
    let sub_dom = match Document::parse(&sub_styles_xml) {
        Ok(d) => d,
        Err(_) => return Ok(renames),
    };

    // collect style ids referenced from the subdoc body
    let mut referenced: HashSet<String> = HashSet::new();
    let mut referenced_order = Vec::new();
    let style_ref_re = crate::patch::re(r#"w:(?:pStyle|rStyle|tblStyle) w:val="([^"]+)""#);
    for cap in style_ref_re.captures_iter(body_xml).flatten() {
        let id = cap[1].to_string();
        if map_by_name {
            if referenced.insert(id.clone()) {
                referenced_order.push(id);
            }
        } else {
            referenced.insert(id);
        }
    }

    let mut sub_styles: Vec<&Element> = Vec::new();
    collect_style_elems(&sub_dom.root, &mut sub_styles);
    let style_by_id: HashMap<String, &Element> = sub_styles
        .iter()
        .filter_map(|e| e.get_attr("w:styleId").map(|id| (id.to_string(), *e)))
        .collect();

    // expand referenced set with basedOn / link / next chains
    let mut queue: Vec<String> = referenced.iter().cloned().collect();
    while let Some(id) = queue.pop() {
        if let Some(el) = style_by_id.get(&id) {
            for tag in ["w:basedOn", "w:link", "w:next"] {
                if let Some(dep) = el.find(tag) {
                    if let Some(v) = dep.get_attr("w:val") {
                        if referenced.insert(v.to_string()) {
                            queue.push(v.to_string());
                        }
                    }
                }
            }
        }
    }

    let pkg = tpl.package.as_mut().ok_or("package not loaded")?;
    if !pkg.contains("word/styles.xml") {
        // master has no styles part: copy sub's styles entirely
        pkg.set("word/styles.xml", sub_styles_xml.clone().into_bytes());
        pkg.ensure_content_type_override("word/styles.xml", STYLES_CT);
        if pkg.rels(DOCUMENT_PART).by_type(rel_type::STYLES).next().is_none() {
            pkg.add_rel(DOCUMENT_PART, rel_type::STYLES, "styles.xml", false);
        }
        tpl.invalidate_part("word/styles.xml");
        return Ok(renames);
    }

    let master_xml = pkg.get_string("word/styles.xml").unwrap_or_default();
    let mut master_dom = match Document::parse(&master_xml) {
        Ok(d) => d,
        Err(_) => return Ok(renames),
    };

    let mut master_styles: Vec<&Element> = Vec::new();
    collect_style_elems(&master_dom.root, &mut master_styles);
    let master_by_id: HashMap<String, &Element> = master_styles
        .iter()
        .filter_map(|e| e.get_attr("w:styleId").map(|id| (id.to_string(), *e)))
        .collect();

    // decide renames for conflicting definitions
    let existing_ids: HashSet<String> = master_by_id.keys().cloned().collect();
    let mut master_by_name: HashMap<&str, &str> = HashMap::new();
    if map_by_name || opts.preserve_styles {
        for mst in &master_styles {
            if let (Some(id), Some(name)) = (mst.get_attr("w:styleId"), style_name(mst)) {
                master_by_name.entry(name).or_insert(id);
            }
        }
    }
    // style ids absorbed by an equivalent master style (no copy is appended)
    let mut absorbed: HashSet<String> = HashSet::new();
    // new style id -> new w:name for renamed copies (docxcompose renames both)
    let mut rename_names: HashMap<String, String> = HashMap::new();
    // docxcompose Composer._preserved_styles, updated only in the preserve
    // branch and written back to the TplCore at the end
    let mut preserved_state: Option<HashMap<String, Vec<String>>> = None;
    if map_by_name {
        // docxcompose preserve_styles=False: style ids are language-specific
        // but names are not, so references are mapped through the style name
        // to the master's style id; a style whose id the master already has
        // is not copied either (the master's definition wins)
        for st in &sub_styles {
            let Some(id) = st.get_attr("w:styleId").map(|s| s.to_string()) else {
                continue;
            };
            if !referenced.contains(&id) || existing_ids.contains(&id) {
                continue;
            }
            if let Some(mid) = style_name(st).and_then(|n| master_by_name.get(n)) {
                renames.insert(id, mid.to_string());
            }
        }
    } else if opts.preserve_styles {
        // docxcompose preserve_styles=True: the subdoc's style is compared
        // (ignoring w:name/w:rsid) with the master's same-named style (its id
        // found through the style name, like docxcompose mapped_style_id) and
        // with copies preserved by earlier merges; an equivalent style is
        // reused, otherwise a copy with an increment_name-suffixed id/name is
        // appended
        let mut preserved = tpl.preserved_styles.clone();
        // master ids including the copies appended by this merge
        // (docxcompose refreshes our_style_ids after each append)
        let mut taken_ids: HashSet<String> = existing_ids.clone();
        // one decision per master-side style id per merge (docxcompose
        // _current_preserved_styles); the bool marks an equivalent-style
        // match (style absorbed, no copy appended)
        let mut current: HashMap<String, (String, bool)> = HashMap::new();
        for st in &sub_styles {
            let Some(id) = st.get_attr("w:styleId").map(|s| s.to_string()) else {
                continue;
            };
            if !referenced.contains(&id) {
                continue;
            }
            let our_id = style_name(st)
                .and_then(|n| master_by_name.get(n))
                .map(|s| s.to_string())
                .unwrap_or_else(|| id.clone());
            if !taken_ids.contains(&our_id) {
                continue; // no conflict: the style is copied as-is below
            }
            if let Some((target, is_match)) = current.get(&our_id) {
                renames.insert(id.clone(), target.clone());
                if *is_match {
                    absorbed.insert(id);
                }
                continue;
            }
            let candidates = preserved
                .get(&our_id)
                .cloned()
                .unwrap_or_else(|| vec![our_id.clone()]);
            let mut matched: Option<String> = None;
            for pid in &candidates {
                if let Some(pel) = master_by_id.get(pid) {
                    if xml_elements_equal(st, pel, &IGNORED_STYLE_TAGS) {
                        matched = Some(pid.clone());
                        break;
                    }
                }
            }
            let is_match = matched.is_some();
            let target = match matched {
                Some(m) => m,
                None => {
                    let mut new_id = increment_name(&our_id);
                    let mut new_name = style_name(st).map(increment_name);
                    while taken_ids.contains(&new_id) {
                        new_id = increment_name(&new_id);
                        if let Some(n) = &mut new_name {
                            *n = increment_name(n);
                        }
                    }
                    taken_ids.insert(new_id.clone());
                    if let Some(n) = new_name {
                        rename_names.insert(new_id.clone(), n);
                    }
                    preserved
                        .entry(our_id.clone())
                        .or_insert_with(|| vec![our_id.clone()])
                        .push(new_id.clone());
                    new_id
                }
            };
            current.insert(our_id, (target.clone(), is_match));
            renames.insert(id.clone(), target);
            if is_match {
                absorbed.insert(id);
            }
        }
        preserved_state = Some(preserved);
    } else {
        for st in &sub_styles {
            let Some(id) = st.get_attr("w:styleId").map(|s| s.to_string()) else {
                continue;
            };
            if !referenced.contains(&id) {
                continue;
            }
            if let Some(&master_st) = master_by_id.get(&id) {
                if !elements_equivalent(st, master_st) {
                    // conflict: rename like docxcompose (id_1, id_2, ...)
                    let mut n = 1;
                    let mut new_id = format!("{}_{}", id, n);
                    while existing_ids.contains(&new_id) || style_by_id.contains_key(&new_id) {
                        n += 1;
                        new_id = format!("{}_{}", id, n);
                    }
                    renames.insert(id, new_id);
                }
            }
        }
    }

    // apply renames to body xml and to merged styles' references (single
    // pass; previously one full scan per renamed style)
    if !renames.is_empty() {
        *body_xml = psub(
            r#"(w:(?:pStyle|rStyle|tblStyle|basedOn|link|next) w:val=")([^"]+)(")"#,
            |m| {
                let old = m.get(2).unwrap().as_str();
                match renames.get(old) {
                    Some(new) => format!(
                        "{}{}{}",
                        m.get(1).unwrap().as_str(),
                        new,
                        m.get(3).unwrap().as_str()
                    ),
                    None => m.get(0).unwrap().as_str().to_string(),
                }
            },
            body_xml,
        );
    }

    // SubdocComposer processes style references in story order and appends
    // a new style's linked style immediately after it. Other composition modes
    // retain their existing source-catalog ordering.
    if map_by_name {
        let mut ranks: HashMap<String, usize> = HashMap::new();
        for id in &referenced_order {
            if !ranks.contains_key(id) {
                ranks.insert(id.clone(), ranks.len());
            }
            if existing_ids.contains(id) || renames.contains_key(id) {
                continue;
            }
            if let Some(link_id) = style_by_id.get(id)
                .and_then(|style| style.find("w:link"))
                .and_then(|link| link.get_attr("w:val"))
            {
                if referenced.contains(link_id) && !ranks.contains_key(link_id) {
                    ranks.insert(link_id.to_string(), ranks.len());
                }
            }
        }
        // Stable sorting preserves the existing order of any other dependencies.
        sub_styles.sort_by_key(|style| {
            style.get_attr("w:styleId")
                .and_then(|id| ranks.get(id))
                .copied()
                .unwrap_or(usize::MAX)
        });
    }

    // append referenced styles that the master does not have (with renames applied)
    let mut added = 0usize;
    for st in sub_styles {
        let Some(id) = st.get_attr("w:styleId").map(|s| s.to_string()) else {
            continue;
        };
        if !referenced.contains(&id) {
            continue;
        }
        if renames.contains_key(&id) && map_by_name {
            continue; // absorbed by the master's same-named style
        }
        if absorbed.contains(&id) {
            continue; // equivalent style already present in the master
        }
        if existing_ids.contains(&id) && !renames.contains_key(&id) {
            continue; // identical definition already present
        }
        let mut st = (*st).clone();
        if let Some(new_id) = renames.get(&id) {
            st.set_attr("w:styleId", new_id);
            if let Some(new_name) = rename_names.get(new_id) {
                if let Some(name_el) = st.find_mut("w:name") {
                    name_el.set_attr("w:val", new_name);
                }
            }
        }
        // remap internal references
        for tag in ["w:basedOn", "w:link", "w:next"] {
            if let Some(dep) = st.find_mut(tag) {
                if let Some(v) = dep.get_attr("w:val") {
                    if let Some(nv) = renames.get(v) {
                        let nv = nv.clone();
                        dep.set_attr("w:val", &nv);
                    }
                }
            }
        }
        master_dom.root.children.push(Node::Elem(st));
        added += 1;
    }
    if added > 0 {
        let xml = master_dom.serialize();
        pkg.set("word/styles.xml", xml.into_bytes());
        tpl.invalidate_part("word/styles.xml");
    }
    if let Some(preserved) = preserved_state {
        tpl.preserved_styles = preserved;
    }
    Ok(renames)
}

/// Cheap equivalence check for style definitions (attribute-order-insensitive
/// enough for practical purposes: compare sorted child serialization).
fn elements_equivalent(a: &Element, b: &Element) -> bool {
    let mut sa = String::new();
    let mut sb = String::new();
    a.serialize(&mut sa);
    b.serialize(&mut sb);
    sa == sb
}

/// Tags ignored when comparing style definitions for equivalence
/// (docxcompose IGNORED_STYLE_TAGS).
const IGNORED_STYLE_TAGS: [&str; 2] = ["w:name", "w:rsid"];

/// docxcompose utils.increment_name: the trailing "_" -separated segment is
/// incremented when it is an integer ("Heading_1" -> "Heading_2"), otherwise
/// "_1" is appended ("Heading" -> "Heading_1", "Heading1" -> "Heading1_1").
fn increment_name(name: &str) -> String {
    let last = match name.rsplit('_').next() {
        Some(l) if !l.is_empty() => l,
        _ => return format!("{}_1", name),
    };
    match last.trim().parse::<i64>() {
        Ok(n) => format!("{}{}", &name[..name.len() - last.len()], n + 1),
        Err(_) => format!("{}_1", name),
    }
}

/// docxcompose utils.xml_element_signature: a canonical recursive
/// representation of an element. The root element's own tag/attributes/text
/// are ignored; child order and attribute order are irrelevant; text is
/// stripped; elements with an ignored tag are skipped at any depth.
fn xml_element_signature(el: &Element, ignored: &[&str], is_root: bool) -> String {
    let mut out = String::new();
    if !is_root {
        out.push_str(el.name.as_str());
        let mut attrs: Vec<(&str, &str)> = el
            .attrs
            .iter()
            .map(|(k, v)| (k.as_str(), v.as_str()))
            .collect();
        attrs.sort();
        for (k, v) in attrs {
            out.push('\u{0}');
            out.push_str(k);
            out.push('\u{1}');
            out.push_str(v);
        }
        // text nodes before the first child element (lxml .text), stripped;
        // later text nodes are tails and ignored (compare_tail=False)
        let mut text = String::new();
        for c in &el.children {
            match c {
                Node::Text(t) => text.push_str(t),
                Node::Elem(_) => break,
            }
        }
        out.push('\u{2}');
        out.push_str(text.trim());
    }
    let mut child_sigs: Vec<String> = Vec::new();
    for c in &el.children {
        if let Node::Elem(e) = c {
            if ignored.iter().any(|t| e.name == *t) {
                continue;
            }
            child_sigs.push(xml_element_signature(e, ignored, false));
        }
    }
    child_sigs.sort();
    for s in child_sigs {
        out.push('\u{3}');
        out.push_str(&s);
    }
    out
}

/// docxcompose utils.xml_elements_equal.
fn xml_elements_equal(a: &Element, b: &Element, ignored: &[&str]) -> bool {
    xml_element_signature(a, ignored, true) == xml_element_signature(b, ignored, true)
}

fn collect_style_elems<'a>(el: &'a Element, out: &mut Vec<&'a Element>) {
    for c in &el.children {
        if let Node::Elem(e) = c {
            if e.name == "w:style" {
                out.push(e);
            }
        }
    }
}

/// The style's name (w:name/@w:val).
fn style_name(style: &Element) -> Option<&str> {
    style.find("w:name").and_then(|n| n.get_attr("w:val"))
}

/// docxcompose retain_formatting_from_default_styles (paragraph styles only,
/// which is all docxcompose supports): when the subdoc's default paragraph
/// style differs from the master's, its run properties are copied onto runs
/// without w:rPr/w:rStyle and its paragraph properties onto paragraphs
/// without w:pPr/w:pStyle, so the merged content keeps its look.
fn retain_formatting_from_default_styles(
    tpl: &mut TplCore,
    sub: &Package,
    body_xml: String,
) -> Result<String, String> {
    let Some(sub_styles_xml) = sub.get_string("word/styles.xml") else {
        return Ok(body_xml);
    };
    let Ok(sub_styles_dom) = Document::parse(&sub_styles_xml) else {
        return Ok(body_xml);
    };
    let mut sub_styles: Vec<&Element> = Vec::new();
    collect_style_elems(&sub_styles_dom.root, &mut sub_styles);
    let Some(default_style) = sub_styles.iter().find(|e| is_default_paragraph_style(e)) else {
        return Ok(body_xml);
    };

    // nothing to retain when the master's default paragraph style is identical
    let pkg = tpl.package.as_ref().ok_or("package not loaded")?;
    if let Some(master_xml) = pkg.get_string("word/styles.xml") {
        if let Ok(master_dom) = Document::parse(&master_xml) {
            let mut master_styles: Vec<&Element> = Vec::new();
            collect_style_elems(&master_dom.root, &mut master_styles);
            if let Some(mds) = master_styles.iter().find(|e| is_default_paragraph_style(e)) {
                if xml_elements_equal(default_style, mds, &[]) {
                    return Ok(body_xml);
                }
            }
        }
    }

    // docxcompose takes `.//w:rPr/*` / `.//w:pPr/*`: the children of ALL
    // descendant rPr/pPr elements of the style, including nested ones (e.g.
    // the paragraph-mark run properties inside w:pPr)
    let run_props = descendant_prop_children(default_style, "w:rPr");
    let para_props = descendant_prop_children(default_style, "w:pPr");
    if run_props.is_empty() && para_props.is_empty() {
        return Ok(body_xml);
    }

    let style_by_id: HashMap<String, &Element> = sub_styles
        .iter()
        .filter_map(|e| e.get_attr("w:styleId").map(|id| (id.to_string(), *e)))
        .collect();

    let mut dom = match parse_body_fragment(&body_xml) {
        Ok(d) => d,
        Err(_) => return Ok(body_xml),
    };
    let ctx = RetainCtx {
        run_props: &run_props,
        para_props: &para_props,
        style_by_id: &style_by_id,
    };
    retain_apply(&mut dom.root, None, &ctx);
    Ok(serialize_body_fragment(&dom))
}

fn is_default_paragraph_style(e: &Element) -> bool {
    e.get_attr("w:type") == Some("paragraph")
        && matches!(e.get_attr("w:default"), Some("1") | Some("true") | Some("on"))
}

struct RetainCtx<'a> {
    run_props: &'a [Element],
    para_props: &'a [Element],
    style_by_id: &'a HashMap<String, &'a Element>,
}

fn retain_apply(el: &mut Element, pstyle: Option<String>, ctx: &RetainCtx) {
    // nearest pStyle on the ancestor chain (docxcompose checks
    // */w:pStyle/@w:val of each ancestor of the run)
    let mut pstyle = pstyle;
    for c in &el.children {
        if let Node::Elem(e) = c {
            if let Some(v) = e.find("w:pStyle").and_then(|p| p.get_attr("w:val")) {
                pstyle = Some(v.to_string());
                break;
            }
        }
    }

    if el.name == "w:p" && !ctx.para_props.is_empty() {
        if el.find("w:pPr").is_none() {
            el.children.insert(0, Node::Elem(Element::new("w:pPr")));
        }
        if let Some(ppr) = el.find_mut("w:pPr") {
            if ppr.find("w:pStyle").is_none() {
                let existing: HashSet<String> = ppr
                    .children
                    .iter()
                    .filter_map(|c| match c {
                        Node::Elem(e) => Some(e.name.to_string()),
                        Node::Text(_) => None,
                    })
                    .collect();
                for pp in ctx.para_props {
                    if !existing.contains(pp.name.as_str()) {
                        ppr.children.push(Node::Elem(pp.clone()));
                    }
                }
            }
        }
    }
    // docxcompose: `.//w:r[not(w:rPr)]` gets an empty w:rPr inserted, then
    // EVERY w:rPr without w:rStyle (including nested paragraph-mark rPr)
    // receives the default style's run properties
    if el.name == "w:r" && !ctx.run_props.is_empty() && el.find("w:rPr").is_none() {
        el.children.insert(0, Node::Elem(Element::new("w:rPr")));
    }
    if el.name == "w:rPr" && !ctx.run_props.is_empty() && el.find("w:rStyle").is_none() {
        // properties already defined by the ancestor paragraph's
        // style are not added (they win over the default style)
        let from_style: HashSet<&str> = pstyle
            .as_deref()
            .and_then(|id| ctx.style_by_id.get(id))
            .map(|st| {
                let mut s = HashSet::new();
                for tag in ["w:rPr", "w:pPr"] {
                    if let Some(x) = st.find(tag) {
                        for c in &x.children {
                            if let Node::Elem(e) = c {
                                s.insert(e.name.as_str());
                            }
                        }
                    }
                }
                s
            })
            .unwrap_or_default();
        for rp in ctx.run_props {
            if !from_style.contains(rp.name.as_str()) {
                el.children.push(Node::Elem(rp.clone()));
            }
        }
    }
    for c in el.children.iter_mut() {
        if let Node::Elem(e) = c {
            retain_apply(e, pstyle.clone(), ctx);
        }
    }
}

/// The element children of all descendant `tag` elements, in document order
/// (docxcompose `.//w:rPr/*` / `.//w:pPr/*`).
fn descendant_prop_children(style: &Element, tag: &str) -> Vec<Element> {
    let mut matches: Vec<&Element> = Vec::new();
    style.iter_descendants(tag, &mut matches);
    let mut out = Vec::new();
    for m in matches {
        for c in &m.children {
            if let Node::Elem(e) = c {
                out.push(e.clone());
            }
        }
    }
    out
}

// ---------------- numbering ----------------

/// Existing identical styles do not cause SubdocComposer to import the child
/// numbering catalog. Inspect before merging styles; newly imported or changed
/// styles, direct numIds and uncertain XML remain on the existing path.
fn only_existing_unchanged_styles(tpl: &TplCore, sub: &Package, xml: &str) -> bool {
    use quick_xml::{events::Event, Reader};
    let Some(master_xml) = tpl.package.as_ref().and_then(|pkg| pkg.get_string("word/styles.xml")) else { return false };
    let Some(sub_xml) = sub.get_string("word/styles.xml") else { return false };
    let Ok(master) = Document::parse(&master_xml) else { return false };
    let Ok(child) = Document::parse(&sub_xml) else { return false };
    let mut master_styles = Vec::new();
    let mut child_styles = Vec::new();
    collect_style_elems(&master.root, &mut master_styles);
    collect_style_elems(&child.root, &mut child_styles);
    let master_by_id: HashMap<&str, &Element> = master_styles.iter()
        .filter_map(|style| style.get_attr("w:styleId").map(|id| (id, *style))).collect();
    let child_by_id: HashMap<&str, &Element> = child_styles.iter()
        .filter_map(|style| style.get_attr("w:styleId").map(|id| (id, *style))).collect();
    let mut reader = Reader::from_str(xml);
    let mut depth = 0usize;
    let mut queue = Vec::new();
    loop {
        let event = match reader.read_event() { Ok(event) => event, Err(_) => return false };
        match event {
            Event::Start(ref element) | Event::Empty(ref element) => {
                let local = element.local_name();
                if local.as_ref() == b"numId" { return false; }
                let style = matches!(local.as_ref(), b"pStyle" | b"rStyle" | b"tblStyle");
                if style && !matches!(element.name().as_ref(), b"w:pStyle" | b"w:rStyle" | b"w:tblStyle") { return false; }
                let mut style_id = None;
                for attribute in element.attributes() {
                    let Ok(attribute) = attribute else { return false };
                    if attribute.key.as_ref() == b"xmlns:w" && attribute.value.as_ref() != b"http://schemas.openxmlformats.org/wordprocessingml/2006/main" { return false; }
                    if style && attribute.key.as_ref() == b"w:val" {
                        let Ok(value) = attribute.unescape_value() else { return false };
                        style_id = Some(value.into_owned());
                    }
                }
                if style {
                    let Some(id) = style_id else { return false };
                    queue.push(id);
                }
                if matches!(event, Event::Start(_)) { depth += 1; }
            }
            Event::End(_) => match depth.checked_sub(1) { Some(next) => depth = next, None => return false },
            Event::Eof => break,
            _ => {}
        }
    }
    if depth != 0 || queue.is_empty() { return false; }
    let mut seen = HashSet::new();
    while let Some(id) = queue.pop() {
        if !seen.insert(id.clone()) { continue; }
        let (Some(master), Some(child)) = (master_by_id.get(id.as_str()), child_by_id.get(id.as_str())) else { return false };
        if !elements_equivalent(master, child) { return false; }
        let Some(name) = style_name(child) else { return false };
        if master_styles.iter().filter(|style| style_name(style).is_some_and(|other|
            other.eq_ignore_ascii_case(name))).count() != 1 { return false; }
        for name in ["w:basedOn", "w:link", "w:next"] {
            if let Some(dependency) = child.find(name).and_then(|element| element.get_attr("w:val")) {
                queue.push(dependency.to_owned());
            }
        }
    }
    true
}

/// Be conservative about alternate prefixes, unclosed elements and parser
/// errors, which may conceal references missed by the lexical `w:` spelling.
fn may_reference_numbering(xml: &str) -> bool {
    use quick_xml::{events::Event, Reader};
    let mut reader = Reader::from_str(xml);
    let mut depth = 0usize;
    loop {
        match reader.read_event() {
            Ok(Event::Start(element)) => {
                if matches!(element.local_name().as_ref(), b"numId" | b"pStyle" | b"rStyle" | b"tblStyle") {
                    return true;
                }
                depth += 1;
            }
            Ok(Event::Empty(element)) => {
                if matches!(element.local_name().as_ref(), b"numId" | b"pStyle" | b"rStyle" | b"tblStyle") {
                    return true;
                }
            }
            Ok(Event::End(_)) => match depth.checked_sub(1) {
                Some(next) => depth = next,
                None => return true,
            },
            Ok(Event::Eof) => return depth != 0,
            Err(_) => return true,
            _ => {}
        }
    }
}

/// Select the one directly referenced numbering instance when there are no
/// style references or auxiliary stories. Other shapes retain the existing
/// importer until their reference ordering and relationships are supported.
fn merge_single_direct_numbering(tpl: &mut TplCore, sub: &Package, body: &str)
    -> Result<Option<String>, String> {
    use quick_xml::{events::Event, Reader};
    let mut reader = Reader::from_str(body);
    let mut depth = 0usize;
    let mut direct_id = None;
    let mut references = 0usize;
    loop {
        let event = match reader.read_event() { Ok(event) => event, Err(_) => return Ok(None) };
        match event {
            Event::Start(ref element) | Event::Empty(ref element) => {
                let local = element.local_name();
                if matches!(local.as_ref(), b"pStyle" | b"rStyle" | b"tblStyle") { return Ok(None); }
                let is_num = local.as_ref() == b"numId";
                if is_num && element.name().as_ref() != b"w:numId" { return Ok(None); }
                let mut number = None;
                for attribute in element.attributes() {
                    let Ok(attribute) = attribute else { return Ok(None) };
                    if attribute.key.as_ref() == b"xmlns:w" && attribute.value.as_ref() != b"http://schemas.openxmlformats.org/wordprocessingml/2006/main" { return Ok(None); }
                    if is_num && attribute.key.as_ref() == b"w:val" {
                        let Ok(value) = attribute.unescape_value() else { return Ok(None) };
                        number = value.parse::<i64>().ok().filter(|value| *value >= 0);
                    }
                }
                if is_num {
                    let Some(number) = number else { return Ok(None) };
                    if direct_id.is_some_and(|id| id != number) { return Ok(None); }
                    direct_id = Some(number);
                    references += 1;
                }
                if matches!(event, Event::Start(_)) { depth += 1; }
            }
            Event::End(_) => match depth.checked_sub(1) { Some(next) => depth = next, None => return Ok(None) },
            Event::Eof => break,
            _ => {}
        }
    }
    let Some(original_id) = direct_id else { return Ok(None) };
    if depth != 0 { return Ok(None); }
    let references_re = crate::patch::re(r#"<w:numId w:val="(\d+)""#);
    if references_re.captures_iter(body).flatten().count() != references { return Ok(None); }
    let integer = tpl.numbering_integer.clone();
    let Some(pkg) = tpl.package.as_ref() else { return Err("package not loaded".to_owned()) };
    let Some(master_xml) = pkg.get_string("word/numbering.xml") else { return Ok(None) };
    let Ok(mut master) = Document::parse(&master_xml) else { return Ok(None) };
    let mut max_abstract = -1;
    let mut max_num = 0;
    find_max_numbering(&master.root, &mut max_abstract, &mut max_num);
    if let Some(integer) = integer {
        let mut definitions = Vec::new();
        master.root.iter_descendants("w:abstractNum", &mut definitions);
        let mut maximum = None::<i64>;
        for definition in definitions {
            if let Some(value) = definition.get_attr("w:abstractNumId") {
                let converted = (integer.0)(value)?;
                maximum = Some(maximum.map_or(converted, |current| current.max(converted)));
            }
        }
        max_abstract = maximum.unwrap_or(-1);
    }
    let Some(new_abstract_id) = max_abstract.checked_add(1) else { return Ok(None) };
    let Some(new_num_id) = max_num.checked_add(1) else { return Ok(None) };
    let Some(sub_xml) = sub.get_string("word/numbering.xml") else { return Ok(None) };
    let Ok(sub_dom) = Document::parse(&sub_xml) else { return Ok(None) };
    let source_num = sub_dom.root.children.iter().find_map(|node| match node {
        Node::Elem(element) if element.name == "w:num"
            && element.get_attr("w:numId").and_then(|value| value.parse::<i64>().ok()) == Some(original_id) => Some(element),
        _ => None,
    });
    let Some(source_num) = source_num else { return Ok(Some(body.to_owned())) };
    let Some(source_abstract_id) = source_num.find("w:abstractNumId")
        .and_then(|element| element.get_attr("w:val"))
        .and_then(|value| value.parse::<i64>().ok()) else { return Ok(None) };
    let source_abstract = sub_dom.root.children.iter().find_map(|node| match node {
        Node::Elem(element) if element.name == "w:abstractNum"
            && element.get_attr("w:abstractNumId").and_then(|value| value.parse::<i64>().ok()) == Some(source_abstract_id) => Some(element),
        _ => None,
    });
    let Some(source_abstract) = source_abstract else { return Ok(None) };
    let mut picture_references = Vec::new();
    source_abstract.iter_descendants("w:lvlPicBulletId", &mut picture_references);
    if !picture_references.is_empty() { return Ok(None); }
    let mut nsids = Vec::new();
    source_abstract.iter_descendants("w:nsid", &mut nsids);
    if nsids.len() > 1 || (!nsids.is_empty() && source_abstract.find("w:nsid").is_none()) {
        return Ok(None);
    }
    let mut abstract_num = source_abstract.clone();
    abstract_num.set_attr("w:abstractNumId", &new_abstract_id.to_string());
    // docxcompose gives the copy a random nsid; derive it from the
    // definition instead so renders are reproducible.
    if abstract_num.find("w:nsid").is_some() {
        let mut existing = Vec::new();
        master.root.iter_descendants("w:nsid", &mut existing);
        let taken: Vec<String> = existing.iter().filter_map(|nsid| nsid.get_attr("w:val").map(str::to_owned)).collect();
        let mut definition = String::new();
        abstract_num.serialize(&mut definition);
        let value = crate::numbering::stable_nsid(definition.as_bytes(), &|nsid| taken.iter().any(|t| t.eq_ignore_ascii_case(nsid)));
        if let Some(nsid) = abstract_num.find_mut("w:nsid") { nsid.set_attr("w:val", &value); }
    }
    let mut num = source_num.clone();
    num.set_attr("w:numId", &new_num_id.to_string());
    num.find_mut("w:abstractNumId").unwrap().set_attr("w:val", &new_abstract_id.to_string());
    let abstract_at = master.root.children.iter().position(|node| matches!(node, Node::Elem(element) if element.name == "w:num")).unwrap_or(0);
    master.root.children.insert(abstract_at, Node::Elem(abstract_num));
    let num_at = master.root.children.iter().rposition(|node| matches!(node, Node::Elem(element) if element.name == "w:num")).unwrap_or(master.root.children.len());
    master.root.children.insert(num_at, Node::Elem(num));
    tpl.package.as_mut().ok_or("package not loaded")?.set("word/numbering.xml", master.serialize().into_bytes());
    let updated = psub(r#"(<w:numId w:val=")(\d+)(")"#, |captures| {
        format!("{}{}{}", &captures[1], new_num_id, &captures[3])
    }, body);
    Ok(Some(updated))
}

/// Merge numbering definitions; returns body xml with remapped numIds.
fn merge_numbering(
    tpl: &mut TplCore,
    sub: &Package,
    mut body_xml: String,
    style_renames: &HashMap<String, String>,
    body_only: bool,
) -> Result<String, String> {
    if (tpl.python_source_contract || tpl.python_subdoc_numbering_contract)
        && !may_reference_numbering(&body_xml) {
        // No direct numbering or style references exist in any merged story.
        // Composer never imports the unused numbering catalog in that case.
        // Styled/numbered stories still need the full reference-mapping port.
        return Ok(body_xml);
    }
    if tpl.python_subdoc_numbering_contract && body_only {
        if let Some(updated) = merge_single_direct_numbering(tpl, sub, &body_xml)? {
            return Ok(updated);
        }
    }
    let Some(sub_num_xml) = sub.get_string("word/numbering.xml") else {
        return Ok(body_xml);
    };
    let sub_dom = match Document::parse(&sub_num_xml) {
        Ok(d) => d,
        Err(_) => return Ok(body_xml),
    };

    let pkg = tpl.package.as_mut().ok_or("package not loaded")?;
    if !pkg.contains("word/numbering.xml") {
        pkg.set("word/numbering.xml", sub_num_xml.clone().into_bytes());
        pkg.ensure_content_type_override("word/numbering.xml", NUMBERING_CT);
        if pkg.rels(DOCUMENT_PART).by_type(rel_type::NUMBERING).next().is_none() {
            pkg.add_rel(DOCUMENT_PART, rel_type::NUMBERING, "numbering.xml", false);
        }
        // numbering picture bullets reference images via numbering.xml.rels
        merge_numbering_pics(pkg, sub);
        return Ok(body_xml);
    }

    // numbering picture bullets
    let pic_rid_map = merge_numbering_pics(pkg, sub);

    let master_xml = pkg.get_string("word/numbering.xml").unwrap_or_default();
    let mut master_dom = match Document::parse(&master_xml) {
        Ok(d) => d,
        Err(_) => return Ok(body_xml),
    };

    // find max abstractNumId and numId in master
    let mut max_abstract: i64 = -1;
    let mut max_num: i64 = 0;
    find_max_numbering(&master_dom.root, &mut max_abstract, &mut max_num);
    let abstract_offset = max_abstract + 1;
    let num_offset = max_num + 1;

    // remap sub numbering: build numId map and adjusted elements
    let mut num_map: HashMap<i64, i64> = HashMap::new();
    let mut new_elems: Vec<Node> = Vec::new();
    for c in &sub_dom.root.children {
        let Node::Elem(e) = c else { continue };
        let mut e = e.clone();
        if e.name == "w:abstractNum" {
            if let Some(id) = e.get_attr("w:abstractNumId").and_then(|v| v.parse::<i64>().ok()) {
                e.set_attr("w:abstractNumId", &(id + abstract_offset).to_string());
            }
            apply_numbering_elem_remaps(&mut e, style_renames, &pic_rid_map);
            new_elems.push(Node::Elem(e));
        } else if e.name == "w:num" {
            let old_id = e.get_attr("w:numId").and_then(|v| v.parse::<i64>().ok());
            if let Some(id) = old_id {
                let new_id = id + num_offset;
                num_map.insert(id, new_id);
                e.set_attr("w:numId", &new_id.to_string());
            }
            if let Some(an) = e.find_mut("w:abstractNumId") {
                if let Some(v) = an.get_attr("w:val").and_then(|v| v.parse::<i64>().ok()) {
                    an.set_attr("w:val", &(v + abstract_offset).to_string());
                }
            }
            apply_numbering_elem_remaps(&mut e, style_renames, &pic_rid_map);
            new_elems.push(Node::Elem(e));
        }
    }
    master_dom.root.children.extend(new_elems);
    let xml = master_dom.serialize();
    pkg.set("word/numbering.xml", xml.into_bytes());

    // remap numId references in the sub body xml (single pass)
    if !num_map.is_empty() {
        body_xml = psub(
            r#"(<w:numId w:val=")(\d+)(")"#,
            |m| {
                let old: i64 = m.get(2).unwrap().as_str().parse().unwrap_or(-1);
                match num_map.get(&old) {
                    Some(new) => format!(
                        "{}{}{}",
                        m.get(1).unwrap().as_str(),
                        new,
                        m.get(3).unwrap().as_str()
                    ),
                    None => m.get(0).unwrap().as_str().to_string(),
                }
            },
            &body_xml,
        );
    }

    Ok(body_xml)
}

/// apply style renames (lvl pStyle) and numPicBullet rid remaps inside a
/// numbering element subtree
fn apply_numbering_elem_remaps(
    el: &mut Element,
    style_renames: &HashMap<String, String>,
    pic_rid_map: &HashMap<String, String>,
) {
    if el.name == "w:pStyle" {
        if let Some(v) = el.get_attr("w:val") {
            if let Some(nv) = style_renames.get(v) {
                let nv = nv.clone();
                el.set_attr("w:val", &nv);
            }
        }
    }
    if el.name == "w:numPicBullet" {
        if let Some(v) = el.get_attr("r:id") {
            if let Some(nv) = pic_rid_map.get(v) {
                let nv = nv.clone();
                el.set_attr("r:id", &nv);
            }
        }
    }
    for c in el.children.iter_mut() {
        if let Node::Elem(e) = c {
            apply_numbering_elem_remaps(e, style_renames, pic_rid_map);
        }
    }
}

/// Copy numbering picture bullet images and remap their rIds.
/// Returns old rid -> new rid map (within the numbering part's rels).
fn merge_numbering_pics(master: &mut Package, sub: &Package) -> HashMap<String, String> {
    let mut map = HashMap::new();
    let sub_rels = sub.rels("word/numbering.xml");
    for rel in &sub_rels.rels {
        if rel.rel_type != rel_type::IMAGE || rel.is_external {
            continue;
        }
        let target = resolve_target("word/numbering.xml", &rel.target);
        let Some(blob) = sub.get(&target) else { continue };
        let (partname, _) = add_image_part(master, blob, &target);
        let rel_target = crate::package::relative_target("word/numbering.xml", &partname);
        let new_rid = master.add_rel("word/numbering.xml", rel_type::IMAGE, &rel_target, false);
        map.insert(rel.id.clone(), new_rid);
    }
    map
}

fn find_max_numbering(el: &Element, max_abstract: &mut i64, max_num: &mut i64) {
    if el.name == "w:abstractNum" {
        if let Some(v) = el.get_attr("w:abstractNumId").and_then(|v| v.parse::<i64>().ok()) {
            *max_abstract = (*max_abstract).max(v);
        }
    } else if el.name == "w:num" {
        if let Some(v) = el.get_attr("w:numId").and_then(|v| v.parse::<i64>().ok()) {
            *max_num = (*max_num).max(v);
        }
    }
    for c in &el.children {
        if let Node::Elem(e) = c {
            find_max_numbering(e, max_abstract, max_num);
        }
    }
}

// ---------------- footnotes ----------------

/// Produce a subdoc note part's content (footnotes/comments) with its image
/// and external relationships remapped into the master package. Style and
/// numbering references inside the content are remapped later, jointly with
/// the body (see subdoc_xml_info).
fn prepare_note_part_content(
    tpl: &mut TplCore,
    sub: &Package,
    part: &str,
) -> Result<Option<String>, String> {
    let Some(mut xml) = sub.get_string(part) else {
        return Ok(None);
    };
    // remap image relationships inside the part (rare but supported by
    // docxcompose's add_referenced_parts)
    let sub_rels = sub.rels(part);
    let mut rid_map: HashMap<String, String> = HashMap::new();
    for rel in &sub_rels.rels {
        let new_rid = if rel.rel_type == rel_type::IMAGE {
            let target = resolve_target(part, &rel.target);
            let Some(blob) = sub.get(&target) else { continue };
            let pkg = tpl.package.as_mut().ok_or("package not loaded")?;
            let (partname, _) = add_image_part(pkg, blob, &target);
            let rel_target = crate::package::relative_target(part, &partname);
            pkg.add_rel(part, rel_type::IMAGE, &rel_target, false)
        } else if rel.is_external {
            let pkg = tpl.package.as_mut().ok_or("package not loaded")?;
            pkg.add_rel(part, &rel.rel_type, &rel.target, true)
        } else {
            continue;
        };
        rid_map.insert(rel.id.clone(), new_rid);
    }
    xml = remap_rids(&xml, &rid_map);
    Ok(Some(xml))
}

/// Merge footnote definitions from the subdoc; returns body xml with
/// remapped w:footnoteReference ids. `fn_content` is the subdoc's
/// footnotes.xml with image rids and style/numbering references already
/// remapped (None when the subdoc has no footnotes part).
fn merge_footnotes(
    tpl: &mut TplCore,
    mut body_xml: String,
    fn_content: Option<String>,
) -> Result<String, String> {
    let Some(sub_fn_xml) = fn_content else {
        return Ok(body_xml);
    };
    // quick check: are there real footnotes (id > 1)?
    let sub_dom = match Document::parse(&sub_fn_xml) {
        Ok(d) => d,
        Err(_) => return Ok(body_xml),
    };
    let mut sub_notes: Vec<&Element> = Vec::new();
    collect_footnotes(&sub_dom.root, &mut sub_notes);
    let has_real = sub_notes.iter().any(|e| {
        e.get_attr("w:id")
            .and_then(|v| v.parse::<i64>().ok())
            .map(|id| id > 1)
            .unwrap_or(false)
    });
    if !has_real {
        return Ok(body_xml);
    }

    let pkg = tpl.package.as_mut().ok_or("package not loaded")?;
    if !pkg.contains("word/footnotes.xml") {
        pkg.set("word/footnotes.xml", sub_fn_xml.clone().into_bytes());
        pkg.ensure_content_type_override("word/footnotes.xml", FOOTNOTES_CT);
        if pkg.rels(DOCUMENT_PART).by_type(rel_type::FOOTNOTES).next().is_none() {
            pkg.add_rel(DOCUMENT_PART, rel_type::FOOTNOTES, "footnotes.xml", false);
        }
        return Ok(body_xml);
    }

    let master_xml = pkg.get_string("word/footnotes.xml").unwrap_or_default();
    let mut master_dom = match Document::parse(&master_xml) {
        Ok(d) => d,
        Err(_) => return Ok(body_xml),
    };
    let mut master_notes: Vec<&Element> = Vec::new();
    collect_footnotes(&master_dom.root, &mut master_notes);
    let max_id = master_notes
        .iter()
        .filter_map(|e| e.get_attr("w:id").and_then(|v| v.parse::<i64>().ok()))
        .max()
        .unwrap_or(1);
    let offset = max_id + 1;

    // id remap for real notes
    let mut id_map: HashMap<i64, i64> = HashMap::new();
    for note in &sub_notes {
        if let Some(id) = note.get_attr("w:id").and_then(|v| v.parse::<i64>().ok()) {
            if id > 1 {
                let mut n = (*note).clone();
                let new_id = id + offset;
                n.set_attr("w:id", &new_id.to_string());
                id_map.insert(id, new_id);
                master_dom.root.children.push(Node::Elem(n));
            }
        }
    }
    let xml = master_dom.serialize();
    pkg.set("word/footnotes.xml", xml.into_bytes());

    if !id_map.is_empty() {
        body_xml = psub(
            r#"(<w:footnoteReference w:id=")(\d+)(")"#,
            |m| {
                let old: i64 = m.get(2).unwrap().as_str().parse().unwrap_or(-1);
                match id_map.get(&old) {
                    Some(new) => format!(
                        "{}{}{}",
                        m.get(1).unwrap().as_str(),
                        new,
                        m.get(3).unwrap().as_str()
                    ),
                    None => m.get(0).unwrap().as_str().to_string(),
                }
            },
            &body_xml,
        );
    }
    Ok(body_xml)
}

fn collect_footnotes<'a>(el: &'a Element, out: &mut Vec<&'a Element>) {
    if el.name == "w:footnote" {
        out.push(el);
        return;
    }
    for c in &el.children {
        if let Node::Elem(e) = c {
            collect_footnotes(e, out);
        }
    }
}

// ---------------- endnotes ----------------

/// Merge endnote definitions from the subdoc; returns body xml with
/// remapped w:endnoteReference ids (same algorithm as merge_footnotes).
fn merge_endnotes(
    tpl: &mut TplCore,
    mut body_xml: String,
    en_content: Option<String>,
) -> Result<String, String> {
    let Some(sub_en_xml) = en_content else {
        return Ok(body_xml);
    };
    // quick check: are there real endnotes (id > 1)?
    let sub_dom = match Document::parse(&sub_en_xml) {
        Ok(d) => d,
        Err(_) => return Ok(body_xml),
    };
    let mut sub_notes: Vec<&Element> = Vec::new();
    collect_endnotes(&sub_dom.root, &mut sub_notes);
    let has_real = sub_notes.iter().any(|e| {
        e.get_attr("w:id")
            .and_then(|v| v.parse::<i64>().ok())
            .map(|id| id > 1)
            .unwrap_or(false)
    });
    if !has_real {
        return Ok(body_xml);
    }

    let pkg = tpl.package.as_mut().ok_or("package not loaded")?;
    if !pkg.contains("word/endnotes.xml") {
        pkg.set("word/endnotes.xml", sub_en_xml.clone().into_bytes());
        pkg.ensure_content_type_override("word/endnotes.xml", ENDNOTE_CT);
        if pkg.rels(DOCUMENT_PART).by_type(rel_type::ENDNOTE).next().is_none() {
            pkg.add_rel(DOCUMENT_PART, rel_type::ENDNOTE, "endnotes.xml", false);
        }
        return Ok(body_xml);
    }

    let master_xml = pkg.get_string("word/endnotes.xml").unwrap_or_default();
    let mut master_dom = match Document::parse(&master_xml) {
        Ok(d) => d,
        Err(_) => return Ok(body_xml),
    };
    let mut master_notes: Vec<&Element> = Vec::new();
    collect_endnotes(&master_dom.root, &mut master_notes);
    let max_id = master_notes
        .iter()
        .filter_map(|e| e.get_attr("w:id").and_then(|v| v.parse::<i64>().ok()))
        .max()
        .unwrap_or(1);
    let offset = max_id + 1;

    // id remap for real notes
    let mut id_map: HashMap<i64, i64> = HashMap::new();
    for note in &sub_notes {
        if let Some(id) = note.get_attr("w:id").and_then(|v| v.parse::<i64>().ok()) {
            if id > 1 {
                let mut n = (*note).clone();
                let new_id = id + offset;
                n.set_attr("w:id", &new_id.to_string());
                id_map.insert(id, new_id);
                master_dom.root.children.push(Node::Elem(n));
            }
        }
    }
    let xml = master_dom.serialize();
    pkg.set("word/endnotes.xml", xml.into_bytes());

    if !id_map.is_empty() {
        body_xml = psub(
            r#"(<w:endnoteReference w:id=")(\d+)(")"#,
            |m| {
                let old: i64 = m.get(2).unwrap().as_str().parse().unwrap_or(-1);
                match id_map.get(&old) {
                    Some(new) => format!(
                        "{}{}{}",
                        m.get(1).unwrap().as_str(),
                        new,
                        m.get(3).unwrap().as_str()
                    ),
                    None => m.get(0).unwrap().as_str().to_string(),
                }
            },
            &body_xml,
        );
    }
    Ok(body_xml)
}

fn collect_endnotes<'a>(el: &'a Element, out: &mut Vec<&'a Element>) {
    if el.name == "w:endnote" {
        out.push(el);
        return;
    }
    for c in &el.children {
        if let Node::Elem(e) = c {
            collect_endnotes(e, out);
        }
    }
}

// ---------------- comments ----------------

const COMMENTS_CT: &str =
    "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml";

/// Merge comment definitions from the subdoc; returns body xml with remapped
/// w:commentRangeStart / w:commentRangeEnd / w:commentReference ids, plus a
/// flag telling whether subdoc comments were merged (gates the
/// commentsExtended/commentsIds merge). `c_content` is the subdoc's
/// comments.xml with image rids and style/numbering references already
/// remapped (None when the subdoc has no comments part or the body has no
/// comment references).
/// (docxcompose does not merge comments at all; this goes beyond parity.)
fn merge_comments(
    tpl: &mut TplCore,
    mut body_xml: String,
    c_content: Option<String>,
) -> Result<(String, bool), String> {
    if !body_xml.contains("w:commentReference") {
        return Ok((body_xml, false));
    }
    let Some(sub_c_xml) = c_content else {
        return Ok((body_xml, false));
    };
    let sub_dom = match Document::parse(&sub_c_xml) {
        Ok(d) => d,
        Err(_) => return Ok((body_xml, false)),
    };
    let mut sub_comments: Vec<&Element> = Vec::new();
    collect_comments(&sub_dom.root, &mut sub_comments);
    if sub_comments.is_empty() {
        return Ok((body_xml, false));
    }

    let pkg = tpl.package.as_mut().ok_or("package not loaded")?;
    if !pkg.contains("word/comments.xml") {
        pkg.set("word/comments.xml", sub_c_xml.clone().into_bytes());
        pkg.ensure_content_type_override("word/comments.xml", COMMENTS_CT);
        if pkg.rels(DOCUMENT_PART).by_type(rel_type::COMMENTS).next().is_none() {
            pkg.add_rel(DOCUMENT_PART, rel_type::COMMENTS, "comments.xml", false);
        }
        return Ok((body_xml, true));
    }

    let master_xml = pkg.get_string("word/comments.xml").unwrap_or_default();
    let mut master_dom = match Document::parse(&master_xml) {
        Ok(d) => d,
        Err(_) => return Ok((body_xml, false)),
    };
    let mut master_comments: Vec<&Element> = Vec::new();
    collect_comments(&master_dom.root, &mut master_comments);
    let max_id = master_comments
        .iter()
        .filter_map(|e| e.get_attr("w:id").and_then(|v| v.parse::<i64>().ok()))
        .max()
        .unwrap_or(-1);
    let offset = max_id + 1;

    let mut id_map: HashMap<i64, i64> = HashMap::new();
    for c in &sub_comments {
        if let Some(id) = c.get_attr("w:id").and_then(|v| v.parse::<i64>().ok()) {
            let mut n = (*c).clone();
            let new_id = id + offset;
            n.set_attr("w:id", &new_id.to_string());
            id_map.insert(id, new_id);
            master_dom.root.children.push(Node::Elem(n));
        }
    }
    let xml = master_dom.serialize();
    pkg.set("word/comments.xml", xml.into_bytes());

    if !id_map.is_empty() {
        body_xml = psub(
            r#"(<w:comment(?:RangeStart|RangeEnd|Reference) w:id=")(\d+)(")"#,
            |m| {
                let old: i64 = m.get(2).unwrap().as_str().parse().unwrap_or(-1);
                match id_map.get(&old) {
                    Some(new) => format!(
                        "{}{}{}",
                        m.get(1).unwrap().as_str(),
                        new,
                        m.get(3).unwrap().as_str()
                    ),
                    None => m.get(0).unwrap().as_str().to_string(),
                }
            },
            &body_xml,
        );
    }
    Ok((body_xml, true))
}

/// Merge the w15 commentsExtended / w16cid commentsIds parts (comment
/// threading/resolved state) from the subdoc. paraId/durableId values are
/// random hex and are kept as-is (cross-document collision ~2^-32).
fn merge_comments_extended(tpl: &mut TplCore, sub: &Package) {
    const PARTS: [(&str, &str, &str, &str); 2] = [
        (
            "word/commentsExtended.xml",
            "application/vnd.ms-word.commentsExtended+xml",
            "http://schemas.microsoft.com/office/2011/relationships/commentsExtended",
            "w15:commentEx",
        ),
        (
            "word/commentsIds.xml",
            "application/vnd.ms-word.commentsIds+xml",
            "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds",
            "w16cid:commentId",
        ),
    ];
    for (part, ct, rt, item_tag) in PARTS {
        let Some(sub_xml) = sub.get_string(part) else {
            continue;
        };
        let pkg = match tpl.package.as_mut() {
            Some(p) => p,
            None => return,
        };
        if !pkg.contains(part) {
            pkg.set(part, sub_xml.into_bytes());
            pkg.ensure_content_type_override(part, ct);
            if pkg.rels(DOCUMENT_PART).by_type(rt).next().is_none() {
                let target = crate::package::relative_target(DOCUMENT_PART, part);
                pkg.add_rel(DOCUMENT_PART, rt, &target, false);
            }
            continue;
        }
        // append the subdoc's items to the master's part
        let master_xml = pkg.get_string(part).unwrap_or_default();
        let Ok(mut master_dom) = Document::parse(&master_xml) else {
            continue;
        };
        let Ok(sub_dom) = Document::parse(&sub_xml) else {
            continue;
        };
        let mut items: Vec<&Element> = Vec::new();
        sub_dom.root.iter_descendants(item_tag, &mut items);
        if items.is_empty() {
            continue;
        }
        for it in items {
            master_dom.root.children.push(Node::Elem(it.clone()));
        }
        pkg.set(part, master_dom.serialize().into_bytes());
    }
}

fn collect_comments<'a>(el: &'a Element, out: &mut Vec<&'a Element>) {
    if el.name == "w:comment" {
        out.push(el);
        return;
    }
    for c in &el.children {
        if let Node::Elem(e) = c {
            collect_comments(e, out);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn single_direct_numbering_selects_one_definition_and_remaps_references() {
        let source = crate::package::minimal_docx("<w:p/>");
        let mut master = Package::from_bytes(&source).unwrap();
        master.set("word/numbering.xml", br#"<w:numbering><w:abstractNum w:abstractNumId="5"/><w:num w:numId="3"><w:abstractNumId w:val="5"/></w:num></w:numbering>"#.to_vec());
        let mut child = Package::from_bytes(&source).unwrap();
        child.set("word/numbering.xml", br#"<w:numbering><w:abstractNum w:abstractNumId="2"><w:nsid w:val="12345678"/></w:abstractNum><w:abstractNum w:abstractNumId="8"/><w:num w:numId="7"><w:abstractNumId w:val="2"/></w:num><w:num w:numId="9"><w:abstractNumId w:val="8"/></w:num></w:numbering>"#.to_vec());
        let mut core = TplCore { python_subdoc_numbering_contract: true, package: Some(master), ..Default::default() };
        let body = "<w:p><w:pPr><w:numPr><w:numId w:val=\"7\"/></w:numPr></w:pPr></w:p>";
        let output = merge_single_direct_numbering(&mut core, &child, body).unwrap().unwrap();
        assert!(output.contains("<w:numId w:val=\"4\"/>"));
        let numbering = core.package.as_ref().unwrap().get_string("word/numbering.xml").unwrap();
        let doc = Document::parse(&numbering).unwrap();
        let abstracts = doc.root.find_all("w:abstractNum");
        let nums = doc.root.find_all("w:num");
        assert_eq!(abstracts.len(), 2);
        assert_eq!(nums.len(), 2);
        let added = abstracts.iter().find(|item| item.get_attr("w:abstractNumId") == Some("6")).unwrap();
        // Derived from the definition, not docxcompose's random draw.
        let nsid = added.find("w:nsid").unwrap().get_attr("w:val").unwrap();
        assert_eq!(nsid.len(), 8);
        assert_ne!(nsid, "12345678");
        let mut again = TplCore { python_subdoc_numbering_contract: true, package: Some(Package::from_bytes(&source).unwrap()), ..Default::default() };
        again.package.as_mut().unwrap().set("word/numbering.xml", br#"<w:numbering><w:abstractNum w:abstractNumId="5"/><w:num w:numId="3"><w:abstractNumId w:val="5"/></w:num></w:numbering>"#.to_vec());
        merge_single_direct_numbering(&mut again, &child, body).unwrap().unwrap();
        assert!(again.package.as_ref().unwrap().get_string("word/numbering.xml").unwrap().contains(&format!("w:val=\"{nsid}\"")));
        assert_eq!(nums[0].get_attr("w:numId"), Some("4"));
        assert_eq!(nums[1].get_attr("w:numId"), Some("3"));
        let before = numbering;
        for unsupported in ["<w:numId w:val=\"7\"/><w:numId w:val=\"9\"/>",
            "<w:pStyle w:val=\"A\"/><w:numId w:val=\"7\"/>", "<x:numId x:val=\"7\"/>"] {
            assert!(merge_single_direct_numbering(&mut core, &child, unsupported).unwrap().is_none());
            assert_eq!(core.package.as_ref().unwrap().get_string("word/numbering.xml").unwrap(), before);
        }
    }

    #[test]
    fn existing_style_guard_rejects_ambiguous_or_numbered_content() {
        let source = crate::package::minimal_docx("<w:p/>");
        let styles = br#"<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:style w:styleId="A"><w:name w:val="Alpha"/></w:style></w:styles>"#;
        let mut master = Package::from_bytes(&source).unwrap();
        master.set("word/styles.xml", styles.to_vec());
        let mut child = Package::from_bytes(&source).unwrap();
        child.set("word/styles.xml", styles.to_vec());
        let core = TplCore { package: Some(master), ..Default::default() };
        let body = "<w:p><w:pPr><w:pStyle w:val=\"A\"/></w:pPr></w:p>";
        assert!(only_existing_unchanged_styles(&core, &child, body));
        assert!(!only_existing_unchanged_styles(&core, &child, &format!("{body}<w:numId w:val=\"1\"/>")));
        assert!(!only_existing_unchanged_styles(&core, &child, "<x:pStyle x:val=\"A\"/>"));
        assert!(!only_existing_unchanged_styles(&core, &child, "<w:p><w:pStyle w:val=\"A\"/>"));
        let duplicate = br#"<w:styles><w:style w:styleId="A"><w:name w:val="Alpha"/></w:style><w:style w:styleId="B"><w:name w:val="alpha"/></w:style></w:styles>"#;
        let mut ambiguous = TplCore { package: core.package.clone(), ..Default::default() };
        ambiguous.package.as_mut().unwrap().set("word/styles.xml", duplicate.to_vec());
        assert!(!only_existing_unchanged_styles(&ambiguous, &child, body));
        child.set("word/styles.xml", br#"<w:styles><w:style w:styleId="A"><w:name w:val="Changed"/></w:style></w:styles>"#.to_vec());
        assert!(!only_existing_unchanged_styles(&core, &child, body));
    }

    #[test]
    fn typed_restart_resolves_nested_style_numid_and_reference_order() {
        let source = crate::package::minimal_docx("<w:p/>");
        let mut package = Package::from_bytes(&source).unwrap();
        package.set("word/styles.xml", br#"<w:styles><w:style w:styleId="List"><w:pPr><w:numPr><w:numId w:val="5"/></w:numPr></w:pPr></w:style></w:styles>"#.to_vec());
        package.set("word/numbering.xml", br#"<w:numbering><w:abstractNum w:abstractNumId="7"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl></w:abstractNum><w:num w:numId="5"><w:abstractNumId w:val="7"/></w:num><w:num w:numId="9"><w:abstractNumId w:val="7"/></w:num></w:numbering>"#.to_vec());
        let mut core = TplCore { python_subdoc_numbering_contract: true,
            package: Some(package), ..Default::default() };
        let body = "<w:p><w:pPr><w:pStyle w:val=\"List\"/></w:pPr></w:p>";
        let rendered = restart_first_numbering(&mut core, body.to_owned(), false).unwrap();
        assert!(rendered.contains("<w:numId w:val=\"10\"/>"));
        let numbering = core.package.unwrap().get_string("word/numbering.xml").unwrap();
        assert!(numbering.find("w:numId=\"10\"").unwrap() < numbering.find("w:numId=\"9\"").unwrap());
        assert!(numbering.contains("<w:startOverride w:val=\"1\"/>"));
    }

    #[test]
    fn typed_numbering_contract_skips_only_unreferenced_catalogs() {
        let source = crate::package::minimal_docx("<w:p/>");
        let mut sub = Package::from_bytes(&source).unwrap();
        sub.set("word/numbering.xml", br#"<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:abstractNum w:abstractNumId="0"/><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>"#.to_vec());
        for (body, imports) in [
            ("<w:p><w:r><w:t>plain</w:t></w:r></w:p>", false),
            ("<w:p><w:pPr><w:numPr><w:numId w:val=\"1\"/></w:numPr></w:pPr></w:p>", true),
            ("<w:p><w:pPr><w:pStyle w:val=\"ListNumber\"/></w:pPr></w:p>", true),
            ("<x:p><x:pPr><x:numId x:val=\"1\"/></x:pPr></x:p>", true),
            ("<w:p><w:r><w:t>&lt;w:numId&gt;</w:t></w:r></w:p>", false),
            ("<w:p><unclosed", true),
            ("<w:p><w:r>", true),
        ] {
            let mut core = TplCore { python_subdoc_numbering_contract: true,
                package: Some(Package::from_bytes(&source).unwrap()), ..Default::default() };
            merge_numbering(&mut core, &sub, body.to_owned(), &HashMap::new(), true).unwrap();
            assert_eq!(core.package.unwrap().contains("word/numbering.xml"), imports, "{body}");
        }
    }

    #[test]
    fn python_subdocument_bookmarks_keep_their_original_ids() {
        let xml = r#"<w:bookmarkStart w:id="0" w:name="first"/><w:bookmarkEnd w:id="0"/><w:bookmarkStart w:id="7" w:name="second"/><w:bookmarkEnd w:id="7"/>"#.to_string();
        let mut python_core = TplCore { python_source_contract: true, bookmark_next_id: Some(3), ..Default::default() };
        assert_eq!(renumber_bookmarks(&mut python_core, xml.clone()), xml);
        assert_eq!(python_core.bookmark_next_id, Some(3));

        let mut native_core = TplCore { bookmark_next_id: Some(3), ..Default::default() };
        let rewritten = renumber_bookmarks(&mut native_core, xml);
        assert!(rewritten.contains(r#"w:id="3""#));
        assert!(rewritten.contains(r#"w:id="10""#));
    }

    fn build_zip(entries: &[(&str, &[u8])]) -> Vec<u8> {
        let mut cursor = std::io::Cursor::new(Vec::new());
        {
            let mut w = zip::ZipWriter::new(&mut cursor);
            for (name, data) in entries {
                w.start_file(name, zip::write::SimpleFileOptions::default())
                    .unwrap();
                w.write_all(data).unwrap();
            }
            w.finish().unwrap();
        }
        cursor.into_inner()
    }

    /// docxcompose utils.increment_name: the trailing "_" -separated segment
    /// is incremented when it is an integer, otherwise "_1" is appended.
    #[test]
    fn test_increment_name() {
        assert_eq!(increment_name("Heading"), "Heading_1");
        assert_eq!(increment_name("Heading_1"), "Heading_2");
        assert_eq!(increment_name("Heading_9"), "Heading_10");
        // trailing digits without a "_" separator are NOT incremented
        // (docxcompose quirk: increment_name splits on "_")
        assert_eq!(increment_name("Heading1"), "Heading1_1");
        assert_eq!(increment_name("X_1_2"), "X_1_3");
        assert_eq!(increment_name(""), "_1");
    }

    /// docxcompose utils.xml_elements_equal: w:name/w:rsid are ignored,
    /// child order is irrelevant, the style's own attributes too.
    #[test]
    fn test_xml_elements_equal_styles() {
        let parse = |xml: &str| Document::parse(xml).unwrap().root;
        let a = parse(
            "<w:style xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\" w:type=\"paragraph\" w:styleId=\"X\"><w:name w:val=\"Foo\"/><w:rPr><w:b/><w:i/></w:rPr></w:style>",
        );
        // different name, extra rsid, different child order, different id
        let b = parse(
            "<w:style xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\" w:type=\"paragraph\" w:styleId=\"X_1\"><w:rsid w:val=\"00ABC\"/><w:name w:val=\"Bar\"/><w:rPr><w:i/><w:b/></w:rPr></w:style>",
        );
        assert!(xml_elements_equal(&a, &b, &IGNORED_STYLE_TAGS));
        assert!(!xml_elements_equal(&a, &b, &[])); // name/rsid count without ignored tags
        let c = parse(
            "<w:style xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\" w:type=\"paragraph\" w:styleId=\"X\"><w:name w:val=\"Foo\"/><w:rPr><w:b/></w:rPr></w:style>",
        );
        assert!(!xml_elements_equal(&a, &c, &IGNORED_STYLE_TAGS));
    }

    /// Binary parts (embeddings, activeX, …) must be copied verbatim: the old
    /// lossy UTF-8 round-trip replaced invalid bytes with U+FFFD and silently
    /// corrupted them.
    #[test]
    fn test_copy_part_recursive_binary_copied_verbatim() {
        // invalid UTF-8 (0xFF/0xFE/0x80…) with no valid decoding
        let blob: &[u8] = &[0xD0, 0xCF, 0x11, 0xE0, 0xFF, 0xFE, 0x80, 0x00, 0xF5, 0xC3];
        let sub = Package::from_bytes(&build_zip(&[(
            "word/embeddings/oleObject1.bin",
            blob,
        )]))
        .unwrap();
        let mut master = Package::from_bytes(&build_zip(&[])).unwrap();

        copy_part_recursive(&mut master, &sub, "word/embeddings/oleObject1.bin", "word/embeddings/oleObject1.bin", 0);

        assert_eq!(
            master.get("word/embeddings/oleObject1.bin"),
            Some(blob.as_ref())
        );
    }

    /// Comments from a subdoc are merged with offset ids and the body
    /// references (range start/end + reference) are remapped accordingly.
    #[test]
    fn test_merge_comments_offsets_ids() {
        const W: &str = "xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"";
        let master_doc = format!("<w:document {W}><w:body><w:p/></w:body></w:document>");
        let master_comments = format!(
            "<w:comments {W}><w:comment w:id=\"0\"><w:p><w:r><w:t>master note</w:t></w:r></w:p></w:comment></w:comments>"
        );
        let master_zip = build_zip(&[
            ("word/document.xml", master_doc.as_bytes()),
            ("word/comments.xml", master_comments.as_bytes()),
        ]);
        let mut core = TplCore::new(master_zip);
        core.init_docx(false).unwrap();

        let sub_comments = format!(
            "<w:comments {W}><w:comment w:id=\"0\"><w:p><w:r><w:t>sub note</w:t></w:r></w:p></w:comment></w:comments>"
        );
        let sub = Package::from_bytes(&build_zip(&[(
            "word/comments.xml",
            sub_comments.as_bytes(),
        )]))
        .unwrap();

        let body = "<w:p><w:commentRangeStart w:id=\"0\"/><w:r><w:t>x</w:t></w:r>\
                    <w:commentRangeEnd w:id=\"0\"/><w:r><w:commentReference w:id=\"0\"/></w:r></w:p>"
            .to_string();
        let (out, merged) =
            merge_comments(&mut core, body, sub.get_string("word/comments.xml")).unwrap();
        assert!(merged);

        assert!(out.contains("<w:commentRangeStart w:id=\"1\"/>"));
        assert!(out.contains("<w:commentRangeEnd w:id=\"1\"/>"));
        assert!(out.contains("<w:commentReference w:id=\"1\"/>"));
        let merged = core
            .package
            .as_ref()
            .unwrap()
            .get_string("word/comments.xml")
            .unwrap();
        assert!(merged.contains("master note"));
        assert!(merged.contains("sub note"));
        assert!(merged.contains("w:id=\"1\""));
    }

    /// The resume break is skipped when the master's last block already ends
    /// a section (otherwise each consecutive keep_sections append would add
    /// an empty section = a blank page).
    #[test]
    fn test_master_resume_sectpr_skips_after_section_break() {
        const W: &str = "xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"";
        let sectpr = "<w:sectPr><w:pgSz w:w=\"11906\" w:h=\"16838\"/></w:sectPr>";
        // plain trailing paragraph -> resume break offered
        let doc1 = format!("<w:document {W}><w:body><w:p/>{sectpr}</w:body></w:document>");
        let mut core1 = TplCore::new(build_zip(&[("word/document.xml", doc1.as_bytes())]));
        core1.init_docx(false).unwrap();
        assert!(master_resume_sectpr(&mut core1).is_some());
        // trailing paragraph-level sectPr -> skipped
        let doc2 = format!(
            "<w:document {W}><w:body><w:p><w:pPr>{sectpr}</w:pPr></w:p>{sectpr}</w:body></w:document>"
        );
        let mut core2 = TplCore::new(build_zip(&[("word/document.xml", doc2.as_bytes())]));
        core2.init_docx(false).unwrap();
        assert!(master_resume_sectpr(&mut core2).is_none());
        // empty body sectPr -> nothing worth resuming
        let doc3 = format!("<w:document {W}><w:body><w:p/><w:sectPr/></w:body></w:document>");
        let mut core3 = TplCore::new(build_zip(&[("word/document.xml", doc3.as_bytes())]));
        core3.init_docx(false).unwrap();
        assert!(master_resume_sectpr(&mut core3).is_none());
    }

    /// commentsExtended items are appended to the master's part (or the part
    /// is copied whole when the master lacks it).
    #[test]
    fn test_merge_comments_extended_append_and_copy() {
        const W15: &str = "xmlns:w15=\"http://schemas.microsoft.com/office/word/2012/wordml\"";
        let sub_ex = format!(
            "<w15:commentsEx {W15}><w15:commentEx w15:paraId=\"AAAA0001\" w15:done=\"1\"/></w15:commentsEx>"
        );
        let sub = Package::from_bytes(&build_zip(&[(
            "word/commentsExtended.xml",
            sub_ex.as_bytes(),
        )]))
        .unwrap();
        let master_doc = b"<w:document/><w:body/>".to_vec();

        // master lacks the part -> copied whole
        let mut core = TplCore::new(build_zip(&[("word/document.xml", &master_doc)]));
        core.init_docx(false).unwrap();
        merge_comments_extended(&mut core, &sub);
        let pkg = core.package.as_ref().unwrap();
        assert!(
            pkg.get_string("word/commentsExtended.xml")
                .unwrap()
                .contains("AAAA0001")
        );

        // master has the part -> subdoc items appended
        let master_ex = format!(
            "<w15:commentsEx {W15}><w15:commentEx w15:paraId=\"BBBB0002\" w15:done=\"0\"/></w15:commentsEx>"
        );
        let mut core2 = TplCore::new(build_zip(&[
            ("word/document.xml", &master_doc),
            ("word/commentsExtended.xml", master_ex.as_bytes()),
        ]));
        core2.init_docx(false).unwrap();
        merge_comments_extended(&mut core2, &sub);
        let merged = core2
            .package
            .as_ref()
            .unwrap()
            .get_string("word/commentsExtended.xml")
            .unwrap();
        assert!(merged.contains("BBBB0002") && merged.contains("AAAA0001"));
    }
}
