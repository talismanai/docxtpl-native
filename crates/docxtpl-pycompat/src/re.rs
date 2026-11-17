//! Python `re` replacement templates (`re.sub(pattern, repl, text)` with a
//! string `repl`).

/// Expand `template` for one match: `\n`-style escapes, `\1`..`\9` and
/// `\g<n>` group references (`group(n)` returns the group text, empty when it
/// did not participate). Errors carry Python's `bad escape` message.
pub fn expand_template(template: &str, group: &dyn Fn(usize) -> String) -> Result<String, String> {
    let bad = |m: &str| format!("bad escape {m}");
    let chars: Vec<char> = template.chars().collect();
    let mut out = String::new();
    let mut i = 0;
    while i < chars.len() {
        if chars[i] != '\\' { out.push(chars[i]); i += 1; continue; }
        let Some(&next) = chars.get(i + 1) else { return Err(bad("(end of pattern)")) };
        i += 2;
        match next {
            'n' => out.push('\n'), 't' => out.push('\t'), 'r' => out.push('\r'), 'a' => out.push('\u{7}'),
            'f' => out.push('\u{c}'), 'v' => out.push('\u{b}'), 'b' => out.push('\u{8}'), '\\' => out.push('\\'),
            'g' => {
                let end = chars[i..].iter().position(|&c| c == '>').ok_or_else(|| bad("\\g"))?;
                let name: String = chars[i + 1..i + end].iter().collect();
                out.push_str(&group(name.parse().map_err(|_| bad("\\g"))?));
                i += end + 1;
            }
            d if d.is_ascii_digit() => out.push_str(&group(d.to_digit(10).expect("digit") as usize)),
            c if c.is_ascii_alphanumeric() => return Err(bad(&format!("\\{c}"))),
            c => { out.push('\\'); out.push(c); }
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::expand_template;

    #[test]
    fn expands_groups_and_escapes_like_python() {
        let group = |n: usize| ["whole", "one"].get(n).copied().unwrap_or_default().to_owned();
        assert_eq!(expand_template(r"[\1|\g<0>]\n\.", &group).unwrap(), "[one|whole]\n\\.");
        assert_eq!(expand_template(r"\q", &group).unwrap_err(), "bad escape \\q");
        assert_eq!(expand_template("\\", &group).unwrap_err(), "bad escape (end of pattern)");
    }
}
