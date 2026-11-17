//! One dependency for a DOCX template pipeline.
//!
//! A pipeline built on this project needs several crates that must agree on
//! their versions: the engine, the MiniJinja the engine's values belong to,
//! and whichever layers the pipeline uses. Depending on each one separately
//! means repeating the same revision in as many places, and a mismatch is
//! only found at the type level, deep in a build. This crate is the single
//! dependency that brings them in together, each behind a feature:
//!
//! | Feature | What it adds |
//! |---------|--------------|
//! | *(always)* | [`docxtplrs`] (the engine) and [`minijinja`] |
//! | `pycompat` | [`pycompat`]: lxml trees, python-docx relationships, pictures, `str`/`repr` |
//! | `wasm` | [`wasm`]: rendering for browsers and embedders, and the WebAssembly ABI |
//! | `markers` | [`markers`]: linking a rendered document back to its template |
//! | `pdf` | [`pdf`]: DOCX to PDF with fonts supplied by the host |
//! | `host-core` | [`core`]: the host core, which calls libxml2 (not for wasm32) |
//! | `browser` | `pycompat`, `wasm`, `markers` and `pdf` together |
//!
//! ```toml
//! [dependencies]
//! docxtpl = { git = "https://github.com/talismanai/docxtpl-native", rev = "...", features = ["browser"] }
//!
//! # Cargo reads a patch table only from the workspace root being built, so
//! # these two cannot come from this crate. They point `jinja`'s MiniJinja
//! # and `flate2`'s zlib-rs at the audited forks, on the same revision.
//! [patch.crates-io]
//! minijinja = { git = "https://github.com/talismanai/docxtpl-native", rev = "..." }
//! zlib-rs = { git = "https://github.com/talismanai/docxtpl-native", rev = "..." }
//! ```

pub use docxtplrs;
pub use minijinja;

#[cfg(feature = "host-core")]
pub use docxtpl_core as core;
#[cfg(feature = "markers")]
pub use docxtpl_markers as markers;
#[cfg(feature = "pdf")]
pub use docxtpl_pdf as pdf;
#[cfg(feature = "pycompat")]
pub use docxtpl_pycompat as pycompat;
#[cfg(feature = "wasm")]
pub use docxtpl_wasm as wasm;
