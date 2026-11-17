//! Python library behaviors that docxtpl-compatible pipelines rely on, for
//! code that must produce the same documents without Python: lxml trees,
//! python-docx relationships, Pillow pictures, `str`/`repr` and `re`
//! replacement templates.

pub mod dom;
pub mod images;
pub mod pyvalue;
pub mod re;
pub mod rels;
