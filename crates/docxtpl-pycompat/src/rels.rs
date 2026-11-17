//! python-docx part relationships: `Part.relate_to(target, reltype,
//! is_external=True)` reuses a matching relationship or takes the first free
//! `rId<n>` for `n` in `1..=len+1`.

use crate::dom::{Attr, Dom, Name, NodeId};

const RL: &str = "http://schemas.openxmlformats.org/package/2006/relationships";
pub const HYPERLINK: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink";

pub struct Relationships {
    dom: Dom,
    root: NodeId,
}

impl Relationships {
    pub fn parse(xml: Option<&str>) -> Result<Relationships, String> {
        let xml = xml.unwrap_or(r#"<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"#);
        let dom = Dom::parse(xml)?;
        let root = dom.document_element().ok_or("relationships have no root")?;
        Ok(Relationships { dom, root })
    }

    fn entries(&self) -> Vec<NodeId> {
        self.dom.children(self.root).into_iter().filter(|&e| self.dom.is(e, RL, "Relationship")).collect()
    }

    pub fn relate_external(&mut self, reltype: &str, target: &str) -> String {
        let entries = self.entries();
        for &entry in &entries {
            let attr = |name: &str| self.dom.attr(entry, None, name).unwrap_or_default();
            if attr("Type") == reltype && attr("TargetMode") == "External" && attr("Target") == target {
                return attr("Id").to_owned();
            }
        }
        let ids: Vec<String> = entries.iter().map(|&e| self.dom.attr(e, None, "Id").unwrap_or_default().to_owned()).collect();
        let id = (1..=entries.len() + 1).map(|n| format!("rId{n}")).find(|c| !ids.contains(c)).expect("free rId");
        let prefix = self.dom.name(self.root).and_then(|n| n.prefix.clone());
        let plain = |local: &str, value: &str| Attr { name: Name { prefix: None, local: local.into(), ns: None }, value: value.into() };
        let element = self.dom.new_element(Name { prefix, local: "Relationship".into(), ns: Some(RL.into()) },
            vec![plain("Id", &id), plain("Type", reltype), plain("Target", target), plain("TargetMode", "External")]);
        self.dom.append(self.root, element);
        id
    }

    pub fn serialize(&self) -> String { self.dom.serialize() }
}
