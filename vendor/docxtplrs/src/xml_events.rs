//! Text events as the parsers of this crate expect them.
//!
//! Since quick-xml 0.38, a reference in character data (`&amp;`, `&#38;`,
//! `&#x26;`, `&name;`) is reported as its own `Event::GeneralRef` between
//! `Event::Text` pieces, and a `&` that starts no reference is an error unless
//! `allow_dangling_amp` is set. The parsers here were written for quick-xml
//! 0.36, which reported each run of character data between two markup events
//! as one `Event::Text` holding the run's source text, escapes included, and
//! accepted a lone `&` there; they unescape that source themselves.
//!
//! [`reader`] accepts a lone `&` again, as text, and [`TextRuns`] joins the
//! pieces of a run back into one `Event::Text` with the run's exact source
//! text. `quick_xml::escape::unescape` therefore receives what it received
//! before and resolves, or rejects, the same references.

use std::borrow::Cow;

use quick_xml::events::{BytesText, Event};
use quick_xml::Reader;

/// A reader over `xml` with quick-xml's defaults, except that a `&` without
/// a closing `;` is text, as it was in quick-xml 0.36.
pub(crate) fn reader(xml: &str) -> Reader<&[u8]> {
    let mut reader = Reader::from_str(xml);
    reader.config_mut().allow_dangling_amp = true;
    reader
}

/// Events of a reader, with every run of character data between two markup
/// events delivered as one `Event::Text` holding the run's source text.
/// Empty runs produce no event.
pub(crate) struct TextRuns<'i> {
    reader: Reader<&'i [u8]>,
    held: Option<quick_xml::Result<Event<'i>>>,
}

impl<'i> TextRuns<'i> {
    pub(crate) fn new(reader: Reader<&'i [u8]>) -> Self {
        TextRuns { reader, held: None }
    }

    pub(crate) fn config_mut(&mut self) -> &mut quick_xml::reader::Config {
        self.reader.config_mut()
    }

    pub(crate) fn read_event(&mut self) -> quick_xml::Result<Event<'i>> {
        if let Some(held) = self.held.take() {
            return held;
        }
        let mut run: Option<Cow<'i, str>> = None;
        loop {
            let piece = match self.reader.read_event() {
                Ok(Event::Text(text)) => text.into_inner(),
                Ok(Event::GeneralRef(reference)) => Cow::Owned(format!("&{};", &*reference)),
                other => {
                    // The run ends at markup, at the end of input or at an
                    // error, which is reported after the text before it.
                    let Some(run) = run else { return other };
                    self.held = Some(other);
                    return Ok(Event::Text(BytesText::from_escaped(run)));
                }
            };
            if piece.is_empty() {
                continue;
            }
            run = Some(match run {
                None => piece,
                Some(mut run) => {
                    run.to_mut().push_str(&piece);
                    run
                }
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn runs(xml: &str) -> Vec<String> {
        let mut events = TextRuns::new(reader(xml));
        let mut out = Vec::new();
        loop {
            match events.read_event() {
                Ok(Event::Text(text)) => out.push(format!("T{}", &*text)),
                Ok(Event::Start(element)) => out.push(format!("S{}", element.name().into_inner())),
                Ok(Event::Empty(element)) => out.push(format!("E{}", element.name().into_inner())),
                Ok(Event::End(element)) => out.push(format!("/{}", element.name().into_inner())),
                Ok(Event::Eof) => break,
                Ok(other) => out.push(format!("?{other:?}")),
                Err(_) => { out.push("!".into()); break; }
            }
        }
        out
    }

    #[test]
    fn a_run_with_references_is_one_text_event_with_its_source() {
        assert_eq!(runs("<a>x &amp; y&lt;&#38;&#x26;&custom;z</a>"),
            ["Sa", "Tx &amp; y&lt;&#38;&#x26;&custom;z", "/a"]);
        assert_eq!(runs("<a>&amp;</a><b/>&lt;tail"), ["Sa", "T&amp;", "/a", "Eb", "T&lt;tail"]);
        assert_eq!(runs("<a></a>"), ["Sa", "/a"]);
    }

    #[test]
    fn a_lone_ampersand_is_text() {
        assert_eq!(runs("<a>R&D</a>"), ["Sa", "TR&D", "/a"]);
        assert_eq!(runs("<a>&a&b;&</a>&"), ["Sa", "T&a&b;&", "/a", "T&"]);
    }

    #[test]
    fn an_error_follows_the_text_before_it() {
        assert_eq!(runs("<a>x&amp;<"), ["Sa", "Tx&amp;", "!"]);
    }
}
