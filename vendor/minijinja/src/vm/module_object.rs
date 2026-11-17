use std::fmt;
use std::sync::{Arc, OnceLock};

use crate::value::{Enumerator, Object, Value, ValueMap};

/// The module an `import` creates: the imported template's exports and the
/// output its body rendered.
#[derive(Debug, Default)]
pub struct Module {
    values: ValueMap,
    captured: Value,
    exports: Option<Value>,
    name: String,
    fragments: Vec<Value>,
    foreign: OnceLock<Value>,
}

impl Module {
    pub(crate) fn new(values: ValueMap, captured: Value, exports: Option<Value>, name: String,
        fragments: Vec<Value>) -> Self {
        Self { values, captured, exports, name, fragments, foreign: OnceLock::new() }
    }

    /// The output of the module's body, joined.
    pub fn captured(&self) -> &Value { &self.captured }

    /// The output of the module's body, as the fragments it was written in.
    pub fn fragments(&self) -> &[Value] { &self.fragments }

    /// The name of the imported template.
    pub fn template_name(&self) -> &str { &self.name }

    /// The exported names and values, in export order.
    pub fn exported_items(self: &Arc<Self>) -> Vec<(Value, Value)> {
        let keys: Vec<Value> = match self.enumerate() {
            Enumerator::Values(keys) => keys,
            _ => Vec::new(),
        };
        keys.into_iter().filter_map(|key| self.get_value(&key).map(|value| (key, value))).collect()
    }

    /// The host object representing this module, created once.
    pub fn foreign_value(&self) -> Option<&Value> { self.foreign.get() }

    /// Record the host object for this module; the first one is kept.
    pub fn set_foreign_value(&self, value: Value) -> &Value { self.foreign.get_or_init(|| value) }
}

impl Object for Module {
    fn get_value(self: &Arc<Self>, key: &Value) -> Option<Value> {
        // Once a host object represents the module, it holds the live exports.
        if let Some(foreign) = self.foreign.get() {
            return foreign.get_attr(key.as_str()?).ok().filter(|value| !value.is_undefined());
        }
        if let Some(exports) = &self.exports {
            return exports.get_item(key).ok().filter(|value| !value.is_undefined());
        }
        self.values.get(key).cloned()
    }

    fn enumerate(self: &Arc<Self>) -> Enumerator {
        if let Some(exports) = &self.exports {
            return Enumerator::Values(exports.try_iter().into_iter().flatten().collect());
        }
        let keys = self.values.keys().cloned();
        Enumerator::Values(keys.collect())
    }

    fn render(self: &Arc<Self>, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.captured)
    }
}
