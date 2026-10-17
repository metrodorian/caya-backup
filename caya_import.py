#!/usr/bin/env python3
"""Legt neue Caya-Dokumente in den Dokumente-Ordner ab (Standard: Trockenlauf).

- Schon lokal vorhandene Dokumente (Datei- oder Inhalts-Hash) werden nur
  vermerkt und nie angefasst.
- Neue Dokumente werden als "JJJJ.MM.TT Absender - Betreff.ext" (Eingangsdatum)
  in den zugeordneten Ordner kopiert (zuordnung.csv).
- Wechselt ein vom Sync abgelegtes Dokument in Caya den Ordner, wird die lokale
  Datei mitverschoben – nur wenn sie unverändert am vermerkten Ort liegt.
- Es wird nie etwas überschrieben oder gelöscht.

    .venv/bin/python caya_import.py           # Trockenlauf -> downloads/import-plan.csv
    .venv/bin/python caya_import.py --apply   # ausführen
"""
import csv
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from caya_compare import DEFAULT_LOCAL, find_local, sha256
from caya_fetch import MANIFEST, OUT, safe

BASE = Path(__file__).resolve().parent
MAPPING = BASE / "zuordnung.csv"            # privat (gitignored), vom Nutzer pflegbar
SUGGESTION = OUT / "ordner-zuordnung.csv"   # Vorschlag aus caya_compare.py
STATE = OUT / "import-state.json"
PLAN = OUT / "import-plan.csv"
FIXED = {"Inbox": "Caya/Inbox"}
TZ = ZoneInfo("Europe/Berlin")
PLACEHOLDER = re.compile(r"^(betreff|subject|sendername|-+)?$|^sehr geehrte", re.I)


# --- Zuordnung -------------------------------------------------------------
def load_mapping() -> dict[str, str]:
    if not MAPPING.exists():
        rows = list(csv.DictReader(SUGGESTION.open(), delimiter=";"))
        with MAPPING.open("w", newline="") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["caya_ordner", "lokaler_ordner"])
            for r in rows:
                w.writerow([r["caya_ordner"], FIXED.get(r["caya_ordner"], r["lokaler_ordner"])])
        print(f"Zuordnung angelegt: {MAPPING.name} (aus dem Vorschlag)")
    return {r["caya_ordner"]: r["lokaler_ordner"]
            for r in csv.DictReader(MAPPING.open(), delimiter=";")}


def target_dir(caya_folder: str, mapping: dict[str, str]) -> str:
    """Zugeordneter Ordner; unbekannte Unterordner erben vom nächsten Elternordner."""
    parts = Path(caya_folder).parts
    for i in range(len(parts), 0, -1):
        key = str(Path(*parts[:i]))
        local = FIXED.get(key) or mapping.get(key)
        if local:
            return str(Path(local, *parts[i:]))
    rest = parts[1:] if parts and parts[0] == "Ablage" else parts
    return str(Path("Caya", *rest))


# --- Dateiname -------------------------------------------------------------
def clean(text: str | None) -> str:
    text = re.sub(r"\s+", " ", text or "").strip(" ,.;:'\"&-_")
    return "" if PLACEHOLDER.match(text) else text


def first_page_text(path: Path) -> str:
    if path.suffix.lower() != ".pdf":
        return ""
    try:
        import logging
        from pypdf import PdfReader
        logging.getLogger("pypdf").setLevel(logging.ERROR)
        return PdfReader(str(path)).pages[0].extract_text() or ""
    except Exception:
        return ""


def known_sender(text: str, senders: list[str]) -> str:
    """Notlösung bei leerem Absender: ein aus anderen Briefen bekannter Absender im Text."""
    low = re.sub(r"\s+", " ", text).lower()
    hits = [(low.find(s.lower()), s) for s in senders if s.lower() in low]
    return min(hits)[1] if hits else ""


def readable(text: str) -> str:
    """Dateinamen-untaugliche Zeichen lesbar ersetzen statt durch '_'."""
    text = text.replace("./.", " gg. ").replace("/", "-").replace("\\", "-")
    text = re.sub(r"\s*:\s*", " ", text)
    text = re.sub(r'[?*"<>|]', "", text)
    return re.sub(r"\s+", " ", text).strip()


def nice_name(d: dict, src: Path, senders: list[str]) -> str:
    when = datetime.fromisoformat(d["createdAt"].replace("Z", "+00:00")).astimezone(TZ)
    sender = clean(d.get("sender"))
    subject = clean(d.get("subject"))
    if not sender:
        sender = known_sender(first_page_text(src), senders)
    if subject and sender and subject.lower().startswith(sender.lower()):
        subject = ""
    title = readable(" - ".join(x for x in (sender, subject) if x)) or "Brief"
    if len(title) > 120:
        title = title[:120].rsplit(" ", 1)[0].rstrip(" ,-")
    return safe(f"{when:%Y.%m.%d} {title}") + src.suffix.lower()


def free_path(path: Path, taken: set[str]) -> Path:
    """Nie überschreiben: bei Kollision ' (2)', ' (3)' … anhängen."""
    cand, n = path, 2
    while cand.exists() or str(cand) in taken:
        cand = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return cand


# --- Ablauf ----------------------------------------------------------------
def main() -> int:
    apply = "--apply" in sys.argv
    local = DEFAULT_LOCAL
    manifest = json.loads(MANIFEST.read_text())
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    mapping = load_mapping()
    # bekannte Absender (mind. 2x in Caya, längste zuerst) für Briefe ohne Absender
    from collections import Counter
    seen = Counter(clean(d.get("sender")) for d in manifest.values())
    senders = sorted((s for s, n in seen.items() if s and n >= 2 and (" " in s or len(s) >= 10)),
                     key=len, reverse=True)

    unknown = {k: v for k, v in manifest.items() if k not in state}
    matches = find_local(unknown, local, quiet=True) if unknown else {}

    plan, taken = [], set()
    for doc_id, d in sorted(manifest.items(), key=lambda kv: kv[1].get("createdAt") or ""):
        st = state.get(doc_id)
        if st is None:
            hits, how = matches[doc_id]
            if hits:
                plan.append(("vorhanden", doc_id, d, None, local / hits[0], how))
                continue
            src = OUT / d["path"]
            dest = free_path(local / target_dir(d["folder"], mapping) / nice_name(d, src, senders), taken)
            taken.add(str(dest))
            plan.append(("neu", doc_id, d, src, dest, ""))
        elif st["origin"] == "import" and st["folder"] != d["folder"]:
            cur = local / st["path"]
            if not cur.exists() or sha256(cur) != st["sha256"]:
                plan.append(("übersprungen", doc_id, d, cur, None,
                             "lokal umbenannt, verschoben oder geändert"))
                continue
            dest = free_path(local / target_dir(d["folder"], mapping) / cur.name, taken)
            taken.add(str(dest))
            plan.append(("verschieben", doc_id, d, cur, dest, f"Caya: {st['folder']} -> {d['folder']}"))

    with PLAN.open("w", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["aktion", "caya_ordner", "von", "nach", "hinweis", "caya_id"])
        for action, doc_id, d, src, dest, note in plan:
            rel = lambda p: "" if p is None else (str(p.relative_to(local)) if p.is_relative_to(local)
                                                   else str(p.relative_to(OUT)))
            w.writerow([action, d["folder"], rel(src), rel(dest), note, doc_id])

    counts = {a: sum(1 for p in plan if p[0] == a)
              for a in ("vorhanden", "neu", "verschieben", "übersprungen")}
    print("Plan:", ", ".join(f"{n} {a}" for a, n in counts.items()), f"-> {PLAN}")
    if not apply:
        print("Trockenlauf – nichts geändert. Ausführen mit --apply")
        return 0

    for action, doc_id, d, src, dest, note in plan:
        if action == "vorhanden":
            state[doc_id] = {"origin": "vorhanden", "path": str(dest.relative_to(local)),
                             "folder": d["folder"]}
        elif action == "neu":
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            state[doc_id] = {"origin": "import", "path": str(dest.relative_to(local)),
                             "sha256": sha256(dest), "folder": d["folder"]}
            print("  +", dest.relative_to(local))
        elif action == "verschieben":
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dest)
            state[doc_id].update(path=str(dest.relative_to(local)), folder=d["folder"])
            print("  >", src.relative_to(local), "->", dest.relative_to(local))
        elif action == "übersprungen":
            print("  ! übersprungen:", note, "|", src.relative_to(local))
            state[doc_id]["folder"] = d["folder"]  # nur einmal melden
        STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    print("Fertig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
