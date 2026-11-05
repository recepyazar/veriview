"""The viewer's Turkish translation covers every user-visible string."""

import re
from pathlib import Path

VIEWER = Path(__file__).resolve().parent.parent / "src" / "veriview" / "viewer.html"
# text that is the same in both languages
NEUTRAL = {"SVG", "VCD", "VeriView", "◇ FSM", "◐", "TR", "Language / Dil"}


def _tr_keys(src: str) -> set[str]:
    block = src[src.index("const TR = {"):src.index("  let LANG")]
    keys = set()
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"\s*:', block):
        keys.add(m.group(1).replace('\\"', '"').replace("\\n", "\n"))
    return keys


def test_every_t_call_has_a_turkish_entry():
    src = VIEWER.read_text()
    keys = _tr_keys(src)
    used = set()
    for m in re.finditer(r'_t\((?:[^"()]*\?\s*)?"((?:[^"\\]|\\.)*)"(?:\s*:\s*"((?:[^"\\]|\\.)*)")?', src):
        for g in m.groups():
            if g:
                used.add(g.replace('\\"', '"').replace("\\n", "\n"))
    # keys inside the kind-map of the cone message
    used |= {"Fan-in cone", "Fan-out cone", "Fan-in + fan-out"}
    missing = sorted(u for u in used if u not in keys)
    assert not missing, missing


def test_static_page_text_is_translated():
    src = VIEWER.read_text()
    keys = _tr_keys(src)
    body = src[src.index("<body>"):src.index("<script>")]
    texts = {m.group(1).strip() for m in re.finditer(r">([^<>{}]*[A-Za-z][^<>{}]*)<", body)}
    texts |= {m.group(1) for m in re.finditer(r'(?:title|placeholder|aria-label)="([^"]+)"', body)}
    texts = {t.replace("&lt;", "<").replace("&gt;", ">") for t in texts if t}
    missing = sorted(t for t in texts if t not in keys and t not in NEUTRAL)
    assert not missing, missing
