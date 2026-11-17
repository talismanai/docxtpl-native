"""Differential fuzz: docxtplrs::recover vs lxml/libxml2 recover mode.

docxtplrs keeps text that libxml2's recover mode drops (intentional
divergence): predefined entities after the first error, a `&` that starts no
valid reference and a `<` that starts no markup stay as text; characters XML
forbids are dropped either way. The oracle therefore applies those text rules
with regular expressions and lets lxml do only the structural recovery.

Usage: python scripts/recover_fuzz.py RECOVER_BINARY [CASES] [SEED]
The binary reads a JSON list of XML strings and prints the recovered list.
"""
import json
import random
import re
import subprocess
import sys

from lxml import etree  # ty: ignore[unresolved-import]

PIECES = ["<w:t>", "</w:t>", "<w:r>", "</w:r>", "<w:p>", "</w:p>", "<w:tab/>", "</w:tabs>", "<mark>", "</mark>", "<ltda>", "text", " ",
          "a&b", "&amp;", "&lt;", "&gt;", "&quot;", "&apos;", "&#38;", "&#x41;", "&#xZZ;", "&nbsp;", "&x ", "& ", "<1", "< ", "\x01",
          "ç", "&#1;", '<w:t xml:space="preserve">', "<!--c-->", "</w:pPr>", "<w:br/>", "{%p if a %}", "\n"]
FORBIDDEN = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
REFERENCE = re.compile(r"&(?:(?:amp|lt|gt|quot|apos);|#([0-9]+);|#x([0-9a-fA-F]+);)")


def is_forbidden(code: int) -> bool:
    return code < 0x20 and code not in (0x9, 0xA, 0xD) or 0xD800 <= code <= 0xDFFF or code in (0xFFFE, 0xFFFF) or code > 0x10FFFF


# Private-use stand-ins: lxml's recover mode drops entity references after an
# error, so text characters reach it as plain characters and come back after.
STAND_IN = {"&": "\ue000", "<": "\ue001", ">": "\ue002", '"': "\ue003", "'": "\ue004"}
ENTITY = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}


def keep_text(xml: str) -> str:
    """Apply the text rules docxtplrs uses, leaving markup to lxml."""
    xml = FORBIDDEN.sub("", xml)
    out = []
    position = 0
    while position < len(xml):
        character = xml[position]
        if character == "&":
            match = REFERENCE.match(xml, position)
            if match is None:
                out.append(STAND_IN["&"])
                position += 1
                continue
            decimal, hexadecimal = match.groups()
            if decimal or hexadecimal:
                code = int(decimal, 10) if decimal else int(hexadecimal, 16)
                if not is_forbidden(code):
                    value = chr(code)
                    out.append(STAND_IN.get(value, value))
            else:
                value = ENTITY[match.group(0)[1:-1]]
                out.append(STAND_IN.get(value, value))
            position = match.end()
            continue
        if character == "<" and not re.match(r"<[A-Za-z_:/!?]", xml[position:]):
            out.append(STAND_IN["<"])
            position += 1
            continue
        out.append(character)
        position += 1
    return "".join(out)


def restore(tree: etree._Element) -> etree._Element:
    back = {v: k for k, v in STAND_IN.items()}
    pattern = re.compile("[\ue000-\ue004]")
    for element in tree.iter():
        if element.text:
            element.text = pattern.sub(lambda m: back[m.group(0)], element.text)
        if element.tail:
            element.tail = pattern.sub(lambda m: back[m.group(0)], element.tail)
    return tree


def main() -> int:
    binary = sys.argv[1]
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
    rng = random.Random(int(sys.argv[3]) if len(sys.argv) > 3 else 11)
    cases = []
    for _ in range(count):
        body = "".join(rng.choice(PIECES) for _ in range(rng.randint(1, 16)))
        cases.append(f'<w:body xmlns:w="urn:w">{body}</w:body>' + rng.choice(["", "<extra/>", "tail", "</w:body>"]))
    recovered = json.loads(subprocess.run([binary], input=json.dumps(cases), capture_output=True, text=True, check=True).stdout)
    bad = 0
    for case, mine in zip(cases, recovered, strict=True):
        recovered = etree.fromstring(keep_text(case).encode(), etree.XMLParser(recover=True))
        expected = etree.tostring(restore(recovered), method="c14n").decode()
        try:
            actual = etree.tostring(etree.fromstring(mine.encode()), method="c14n").decode()
        except etree.XMLSyntaxError as error:
            actual = f"NOT WELL-FORMED: {error}"
        if expected != actual:
            bad += 1
            if bad <= 8:
                print("DIFF:", repr(case), "\n   lxml:", expected, "\n   mine:", actual)
    print(f"{count - bad}/{count} recover results identical to lxml on text-preserving input")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
