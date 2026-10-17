#!/usr/bin/env python3
"""Vergleicht alle geladenen Caya-Dokumente mit dem Dokumente-Ordner.

Zwei Stufen:
  1. Datei-Hash (SHA-256): gehasht werden nur Dateien, deren Größe zu einem
     Caya-Dokument passt – identische Hashes setzen identische Größen voraus.
  2. Inhalts-Hash für PDFs: Seiteninhalte und eingebettete Bilder, ohne die
     PDF-Hülle. Caya liefert ältere Dokumente heute mit geänderter Hülle aus,
     der Datei-Hash alter Kopien passt dann nicht mehr, der Inhalts-Hash schon.
     Ergebnisse werden in downloads/.inhalt-cache.json zwischengespeichert.

Ausgabe in downloads/:
  abgleich.csv          je Caya-Dokument: vorhanden/neu + lokale Fundorte
  ordner-zuordnung.csv  Vorschlag Caya-Ordner -> lokaler Ordner (zum Korrigieren)

    .venv/bin/python caya_compare.py [lokaler-ordner]
"""
import csv
import difflib
import hashlib
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from caya_fetch import MANIFEST, OUT

DEFAULT_LOCAL = Path.home() / "MEGA/MEGAsync/Dokumente"
CACHE = OUT / ".inhalt-cache.json"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_hash(path: str) -> str | None:
    """Hash über Seiteninhalte und Bilder eines PDFs; None, wenn nicht lesbar."""
    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        reader = PdfReader(path)
        h = hashlib.sha256()
        for page in reader.pages:
            contents = page.get_contents()
            h.update(hashlib.sha256(contents.get_data() if contents else b"").digest())
            xobjects = (page.get("/Resources") or {}).get("/XObject") or {}
            images = []
            for key in xobjects:
                obj = xobjects[key].get_object()
                if obj.get("/Subtype") == "/Image":
                    images.append(hashlib.sha256(obj._data).hexdigest())
            for img in sorted(images):
                h.update(img.encode())
            h.update(b"|")
        return h.hexdigest() if reader.pages else None
    except Exception:
        return None


def content_hashes(paths: list[Path]) -> dict[str, str | None]:
    """Inhalts-Hashes mit Cache (Schlüssel: Pfad + Größe + Änderungszeit)."""
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    keyed = {}
    for p in paths:
        st = p.stat()
        keyed[str(p)] = f"{p}|{st.st_size}|{int(st.st_mtime)}"
    todo = [p for p, k in keyed.items() if k not in cache]
    if todo:
        print(f"Inhalts-Hash für {len(todo)} PDFs berechnen "
              f"({len(paths) - len(todo)} aus dem Cache) …", flush=True)
        with ProcessPoolExecutor() as pool:
            for i, (p, h) in enumerate(zip(todo, pool.map(content_hash, todo, chunksize=8)), 1):
                cache[keyed[p]] = h
                if i % 250 == 0:
                    print(f"  {i}/{len(todo)}", flush=True)
        CACHE.write_text(json.dumps(cache))
    return {p: cache[k] for p, k in keyed.items()}


def local_folders(local: Path) -> list[str]:
    """Alle lokalen Ordner (relativ), für den Namensvergleich."""
    out = []
    for dirpath, dirnames, _ in os.walk(local):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        rel = Path(dirpath).relative_to(local)
        if str(rel) != ".":
            out.append(str(rel))
    return out


def best_name_match(caya_folder: str, folders: list[str]) -> tuple[str, float]:
    leaf = Path(caya_folder).name.lower()
    best, score = "", 0.0
    for f in folders:
        s = difflib.SequenceMatcher(None, leaf, Path(f).name.lower()).ratio()
        # bei gleichem Wert den kürzeren (höher liegenden) Pfad bevorzugen
        if s > score or (s == score and best and len(f) < len(best)):
            best, score = f, s
    return best, score


def find_local(manifest: dict, local: Path, quiet: bool = False) -> dict:
    """Caya-ID -> (lokale Fundorte relativ zu local, Trefferart) für alle Dokumente."""
    sizes = {v["size"] for v in manifest.values()}

    # 1) lokale Dateien: Datei-Hash bei passender Größe, PDFs für den Inhalts-Hash sammeln
    by_hash = defaultdict(list)
    pdfs = []
    scanned = hashed = 0
    for dirpath, dirnames, filenames in os.walk(local):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            p = Path(dirpath) / fn
            scanned += 1
            try:
                if p.is_symlink():
                    continue
                if fn.lower().endswith(".pdf"):
                    pdfs.append(p)
                if p.stat().st_size in sizes:
                    by_hash[sha256(p)].append(str(p.relative_to(local)))
                    hashed += 1
            except OSError:
                continue
    if not quiet:
        print(f"{scanned} lokale Dateien durchsucht, {hashed} mit passender Größe gehasht, "
              f"{len(pdfs)} PDFs", flush=True)

    caya_paths = {doc_id: OUT / d["path"] for doc_id, d in manifest.items()}
    hashes = content_hashes(pdfs + list(caya_paths.values()))
    by_content = defaultdict(list)
    for p in pdfs:
        if hashes[str(p)]:
            by_content[hashes[str(p)]].append(str(p.relative_to(local)))

    result = {}
    for doc_id, d in manifest.items():
        hits, how = by_hash.get(d["sha256"], []), "Datei-Hash"
        if not hits:
            ch = hashes.get(str(caya_paths[doc_id]))
            hits, how = (by_content.get(ch, []) if ch else []), "Inhalts-Hash"
        result[doc_id] = (hits, how if hits else "")
    return result


def main() -> int:
    local = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LOCAL
    manifest = json.loads(MANIFEST.read_text())
    matches = find_local(manifest, local)

    # Caya-Dokumente zuordnen
    rows, per_folder = [], defaultdict(Counter)
    found = Counter()
    for doc_id, d in sorted(manifest.items(), key=lambda kv: (kv[1]["folder"], kv[1]["path"])):
        hits, how = matches[doc_id]
        if hits:
            found[how] += 1
        for h in hits:
            per_folder[d["folder"]][str(Path(h).parent)] += 1
        rows.append({"status": "vorhanden" if hits else "neu",
                     "treffer_per": how if hits else "", "caya_ordner": d["folder"],
                     "eingang": (d.get("createdAt") or "")[:10], "absender": d.get("sender") or "",
                     "betreff": d.get("subject") or "", "caya_datei": d["path"],
                     "lokal": " | ".join(hits), "caya_id": doc_id})
    with (OUT / "abgleich.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=";")
        w.writeheader()
        w.writerows(rows)

    # 3) Ordnerzuordnung vorschlagen
    folders = local_folders(local)
    total = Counter(d["folder"] for d in manifest.values())
    mapping = []
    for cf in sorted(total):
        if per_folder[cf]:
            target, n = per_folder[cf].most_common(1)[0]
            basis, info = "Hash-Treffer", f"{n} von {sum(per_folder[cf].values())} Treffern"
        else:
            target, score = best_name_match(cf, folders)
            if score >= 0.8:
                basis, info = "Name", f"Ähnlichkeit {score:.2f}"
            else:
                target, basis, info = "", "OFFEN", (f"bester Name: {target} ({score:.2f})"
                                                    if target else "")
        mapping.append({"caya_ordner": cf, "dokumente": total[cf], "lokaler_ordner": target,
                        "basis": basis, "hinweis": info})
    with (OUT / "ordner-zuordnung.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(mapping[0]), delimiter=";")
        w.writeheader()
        w.writerows(mapping)

    total_found = sum(found.values())
    print(f"{len(manifest)} Caya-Dokumente: {total_found} schon lokal vorhanden "
          f"({found['Datei-Hash']} per Datei-Hash, {found['Inhalts-Hash']} per Inhalts-Hash), "
          f"{len(manifest) - total_found} neu")
    print(f"-> {OUT / 'abgleich.csv'}\n-> {OUT / 'ordner-zuordnung.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
