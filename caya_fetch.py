#!/usr/bin/env python3
"""Lädt alle Caya-Dokumente (Inbox + Ablage samt Unterordnern) herunter.

Ziel: downloads/<Inbox|Ablage>/<Ordnerpfad>/<Caya-Dateiname>
Manifest: downloads/manifest.json (Caya-ID -> Pfad, Hash, Metadaten).
Bereits geladene Dokumente (gleiche ID, Datei vorhanden) werden übersprungen.

    .venv/bin/python caya_fetch.py
"""
import hashlib
import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from caya_api import BASE, Caya, SessionExpired

OUT = BASE / "downloads"
MANIFEST = OUT / "manifest.json"
EXT = {"PDF": ".pdf", "JPG": ".jpg", "JPEG": ".jpg", "PNG": ".png", "TIFF": ".tif"}

log = logging.getLogger("caya-fetch")


def safe(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return name[:180] or "unbenannt"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def walk(caya: Caya, folder_id: str, rel: Path):
    yield folder_id, rel
    for sub in caya.subfolders(folder_id):
        yield from walk(caya, sub["id"], rel / safe(sub["title"]))


def target_name(doc: dict) -> str:
    name = safe(doc.get("filename") or doc["id"])
    ext = EXT.get((doc.get("fileType") or "").upper(), "")
    if ext and not name.lower().endswith(ext):
        name += ext
    return name


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    OUT.mkdir(exist_ok=True)
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    caya = Caya()
    pool = ThreadPoolExecutor(max_workers=4)
    stats = {"neu": 0, "vorhanden": 0, "verschoben": 0, "fehler": 0, "ohne_datei": 0}

    def fetch(doc: dict, rel: Path, folder_path: str):
        dest = OUT / rel / target_name(doc)
        # Namenskollision mit anderem Dokument -> ID anhängen
        others = {v["path"] for k, v in manifest.items() if k != doc["id"]}
        if str(dest.relative_to(OUT)) in others or (dest.exists() and doc["id"] not in manifest):
            dest = dest.with_name(f"{dest.stem} [{doc['id']}]{dest.suffix}")
        try:
            caya.download(doc["file"], dest)
        except Exception as e:
            stats["fehler"] += 1
            log.error("FEHLER %s/%s: %s", folder_path, dest.name, e)
            return
        meta = doc.get("metadata") or {}
        manifest[doc["id"]] = {
            "path": str(dest.relative_to(OUT)), "folder": folder_path,
            "sha256": sha256(dest), "size": dest.stat().st_size,
            "createdAt": doc.get("createdAt"),
            "scanDate": (doc.get("scanMetadata") or {}).get("date"),
            "sender": meta.get("senderName"), "subject": meta.get("subject"),
            "tags": meta.get("tags"), "caya_filename": doc.get("filename"),
        }
        stats["neu"] += 1
        log.info("  + %s", dest.relative_to(OUT))

    try:
        for root_name, root_id in caya.roots().items():
            for folder_id, rel in walk(caya, root_id, Path(root_name)):
                folder_path = str(rel)
                count = 0
                for page in caya.documents(folder_id):
                    jobs = []
                    for doc in page:
                        count += 1
                        known = manifest.get(doc["id"])
                        if known and (OUT / known["path"]).exists():
                            stats["vorhanden"] += 1
                            if known["folder"] != folder_path:  # in Caya verschoben
                                log.info("  ~ verschoben: %s -> %s", known["folder"], folder_path)
                                known["folder"] = folder_path
                                stats["verschoben"] += 1
                            continue
                        if not doc.get("file") or doc.get("isDestroyed"):
                            stats["ohne_datei"] += 1
                            log.warning("  ohne Datei: %s/%s", folder_path, doc.get("filename"))
                            continue
                        jobs.append(pool.submit(fetch, doc, rel, folder_path))
                    for j in jobs:  # Seite fertig laden, solange die Links gültig sind
                        j.result()
                    MANIFEST.write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
                log.info("Ordner %s: %d Dokumente", folder_path, count)
    except SessionExpired as e:
        log.error("%s", e)
        return 3
    finally:
        pool.shutdown()
        MANIFEST.write_text(json.dumps(manifest, indent=1, ensure_ascii=False))

    log.info("Fertig: %(neu)d neu, %(vorhanden)d schon da (%(verschoben)d verschoben), "
             "%(ohne_datei)d ohne Datei, "
             "%(fehler)d Fehler", stats)
    return 1 if stats["fehler"] else 0


if __name__ == "__main__":
    sys.exit(main())
