#!/usr/bin/env python3
"""Erkundet die Caya-Oberfläche mit der gespeicherten Sitzung (nur lesen).

Schreibt nach <ausgabe>/: Screenshot, Seitentext, interaktive Elemente und
die API-Aufrufe der Web-App (Methode, Pfad, Status, Typ – ohne Inhalte).

    .venv/bin/python caya_explore.py <ausgabe> [pfad]
"""
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
APP = "https://app.caya.com"
STATE = BASE / ".auth" / "state.json"


def main() -> int:
    out = Path(sys.argv[1])
    path = sys.argv[2] if len(sys.argv) > 2 else "/"
    out.mkdir(parents=True, exist_ok=True)
    calls = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(storage_state=str(STATE), locale="de-DE",
                                  viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        def on_response(r):
            if r.request.resource_type in ("xhr", "fetch"):
                u = urlsplit(r.url)
                calls.append({"method": r.request.method, "host": u.netloc,
                              "path": u.path, "query_keys": sorted(
                                  k.split("=")[0] for k in u.query.split("&") if k),
                              "status": r.status,
                              "type": r.headers.get("content-type", "")})

        page.on("response", on_response)
        page.goto(APP + path, wait_until="networkidle")
        page.wait_for_timeout(2000)

        if "/login" in page.url:
            print("Sitzung abgelaufen – erst caya_login.py ausführen", file=sys.stderr)
            return 1

        page.screenshot(path=str(out / "seite.png"), full_page=True)
        (out / "text.txt").write_text(page.inner_text("body"))
        elems = page.eval_on_selector_all(
            "a, button, [role=button], [role=treeitem], [role=row], [data-testid]",
            """els => els.map(e => ({tag: e.tagName, testid: e.dataset.testid || null,
                 role: e.getAttribute('role'), href: e.getAttribute('href'),
                 text: (e.innerText || e.getAttribute('aria-label') || '').trim().slice(0, 80)}))""")
        (out / "elemente.json").write_text(json.dumps(elems, indent=1, ensure_ascii=False))
        (out / "api.json").write_text(json.dumps(calls, indent=1))
        print(f"URL: {page.url}\n{len(elems)} Elemente, {len(calls)} API-Aufrufe -> {out}")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
