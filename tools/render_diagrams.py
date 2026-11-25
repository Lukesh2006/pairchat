"""Render docs/diagrams/*.mmd (Mermaid) to SVG and PNG.

    pip install -r tools/requirements-demo.txt
    python tools/render_diagrams.py        # needs internet once (loads mermaid from jsDelivr)

The README embeds the same Mermaid sources, which GitHub renders natively;
the PNG/SVG copies are for slides, reports and viewers without Mermaid.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DIAGRAMS = ROOT / "docs" / "diagrams"
MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"

PAGE = f"""<!doctype html><html><head><meta charset="utf-8">
<script src="{MERMAID}"></script>
<style>body{{margin:0;background:#fff;font-family:"Segoe UI",Arial,sans-serif}}#out{{display:inline-block;padding:24px}}</style>
</head><body><div id="out"></div></body></html>"""


def main(browser_channel: str = "msedge") -> None:
    sources = sorted(DIAGRAMS.glob("*.mmd"))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=browser_channel)
        page = browser.new_page(viewport={"width": 1600, "height": 1200}, device_scale_factor=2)
        page.set_content(PAGE)
        page.wait_for_function("window.mermaid !== undefined")
        page.evaluate("mermaid.initialize({startOnLoad:false, theme:'default', securityLevel:'strict', "
                      "themeVariables:{fontSize:'16px'}, flowchart:{curve:'basis'}})")
        for src in sources:
            svg = page.evaluate(f"async () => (await mermaid.render('d{abs(hash(src.name))}', {json.dumps(src.read_text(encoding='utf-8'))})).svg")
            src.with_suffix(".svg").write_text(svg, encoding="utf-8")
            page.evaluate("svg => { document.getElementById('out').innerHTML = svg; }", svg)
            page.locator("#out").screenshot(path=str(src.with_suffix(".png")))
            print("rendered", src.stem)
        browser.close()


if __name__ == "__main__":
    main(*(sys.argv[1:2]))
