"""The overlay pages show and hide elements with the hidden attribute. Any author display rule
(label{display:block}, .row{display:flex}, an inline style="display:flex") beats the browser's
own [hidden]{display:none}, so such an element never hides: the update banner stayed on
"업데이트 준비 중…" forever and both audio device fields always showed.

Static checks (no browser): every page that uses hidden restates [hidden] with !important, and
no element carries both hidden and an inline display. tests/test_admin_update_banner.mjs runs
the banner logic itself.
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "overlay"


class _Scan(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_with_display: list[str] = []
        self.hidden_ids: list[str] = []
        self.stylesheets: list[str] = []
        self.styles: list[str] = []
        self.scripts: list[str] = []
        self._in: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): (value or "") for name, value in attrs}
        if "hidden" in values:
            self.hidden_ids.append(values.get("id") or tag)
            if re.search(r"(^|;)\s*display\s*:", values.get("style", ""), re.IGNORECASE):
                self.hidden_with_display.append(values.get("id") or tag)
        if tag == "link" and "stylesheet" in values.get("rel", "").lower():
            self.stylesheets.append(values.get("href", ""))
        if tag in ("style", "script"):
            self._in = tag

    def handle_endtag(self, tag: str) -> None:
        if tag == self._in:
            self._in = None

    def handle_data(self, data: str) -> None:
        if self._in == "style":
            self.styles.append(data)
        elif self._in == "script":
            self.scripts.append(data)


def _rules(css: str) -> list[tuple[list[str], str]]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return [
        ([part.strip() for part in selector.split(",")], body)
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    ]


def has_global_hidden_rule(css: str) -> bool:
    for selectors, body in _rules(css):
        if "[hidden]" in selectors and re.search(r"display\s*:\s*none\s*!important", body):
            return True
    return False


def main() -> int:
    pages = sorted(OVERLAY.glob("*.html"))
    assert pages, "no overlay pages found"
    checked = 0
    for page in pages:
        scan = _Scan()
        scan.feed(page.read_text(encoding="utf-8"))
        name = page.name
        assert not scan.hidden_with_display, f"{name}: hidden plus inline display never hides {scan.hidden_with_display}"
        script = "\n".join(scan.scripts)
        uses_hidden = bool(scan.hidden_ids) or re.search(r"\.hidden\s*=", script) is not None
        if not uses_hidden:
            continue
        css = "\n".join(scan.styles)
        for href in scan.stylesheets:
            local = OVERLAY / href
            if not href.startswith(("http:", "https:", "//")) and local.is_file():
                css += "\n" + local.read_text(encoding="utf-8")
        assert has_global_hidden_rule(css), f"{name} uses hidden but has no [hidden]{{display:none !important}} rule"
        checked += 1

    admin = (OVERLAY / "admin.html").read_text(encoding="utf-8")
    banner = re.search(r'<div id="updateBanner"[^>]*>', admin)
    assert banner and " hidden" in banner.group(0) and "style=" not in banner.group(0), banner and banner.group(0)
    assert re.search(r'<div id="updateProgress"[^>]*\shidden', admin), "the update progress bar starts hidden"
    assert "progress.style.display" not in admin, "the progress bar is shown and hidden with the hidden attribute"
    # The two device fields are <label>s, which admin-theme.css displays as blocks.
    for field in ("microphoneDeviceField", "systemAudioDeviceField"):
        assert re.search(rf'<label id="{field}"', admin), field
        assert f'getElementById("{field}").hidden' in admin, field
    theme = (OVERLAY / "admin-theme.css").read_text(encoding="utf-8")
    assert re.search(r"(^|\n)label\s*\{[^}]*display\s*:\s*block", theme), "label display rule moved; recheck this test"
    assert has_global_hidden_rule(theme)
    # The rule must not be scoped to one panel again (the old .engine-panel [hidden] patch).
    assert not has_global_hidden_rule(".engine-panel [hidden] { display: none !important; }")
    assert has_global_hidden_rule("a{color:red}\n[hidden] {\n  display: none !important;\n}")
    print(f"admin_hidden_css_ok ({checked} pages use hidden)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
