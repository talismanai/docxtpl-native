//! Offline diagnostic for the root symbol planner. This does not render.
#[cfg(feature = "unstable_machinery")]
fn main() {
    use minijinja::machinery::{parse, root_symbol_loads, RootLoadKind, WhitespaceConfig};
    use minijinja::syntax::SyntaxConfig;

    let source = std::env::args().nth(1).expect("template source argument");
    match parse(&source, "root-symbol-probe", SyntaxConfig::default(), WhitespaceConfig::default()) {
        Ok(template) => for item in root_symbol_loads(&template) {
            let kind = match item.load {
                RootLoadKind::Resolve => "resolve",
                RootLoadKind::Missing => "missing",
                RootLoadKind::SelfReference => "self",
            };
            println!("{kind}:{}", item.name);
        },
        Err(error) => { eprintln!("{error}"); std::process::exit(2); }
    }
}

#[cfg(not(feature = "unstable_machinery"))]
fn main() {
    eprintln!("root_symbol_probe requires the unstable_machinery feature");
    std::process::exit(2);
}
