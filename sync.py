#!/usr/bin/env python3
"""Sichert einen Google-Drive-Ordner (Caya-Zielordner) in einen lokalen Ordner.

Nur-Lesen gegenüber Drive, löscht lokal nie etwas (reines Backup).
Konfiguration über config.json neben dem Skript (siehe config.example.json).
"""
import io
import json
import logging
import re
import sys
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER_MIME = "application/vnd.google-apps.folder"
BASE = Path(__file__).resolve().parent

log = logging.getLogger("caya-sync")


def load_config() -> dict:
    cfg = json.loads((BASE / "config.json").read_text())
    return {
        "folder_id": cfg["folder_id"],
        "target_dir": Path(cfg["target_dir"]),
        "key_file": Path(cfg.get("key_file", BASE / "service-account.json")),
        "state_file": Path(cfg.get("state_file", BASE / "state.json")),
    }


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip() or "unbenannt"


def list_children(service, folder_id: str):
    page = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType, md5Checksum, modifiedTime)",
            pageSize=200,
            pageToken=page,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        yield from resp.get("files", [])
        page = resp.get("nextPageToken")
        if not page:
            break


def walk(service, folder_id: str, rel: Path = Path()):
    """Liefert (datei, relativer Zielordner) rekursiv."""
    for f in list_children(service, folder_id):
        if f["mimeType"] == FOLDER_MIME:
            yield from walk(service, f["id"], rel / safe_name(f["name"]))
        elif not f["mimeType"].startswith("application/vnd.google-apps."):
            # Google-eigene Formate (Docs/Sheets) haben keine Binärdatei -> überspringen
            yield f, rel


def download(service, file_id: str, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        with part.open("wb") as fh:
            req = service.files().get_media(fileId=file_id, supportsAllDrives=True)
            dl = MediaIoBaseDownload(fh, req, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk()
        part.replace(dest)  # atomar: nie halbe Dateien im Backup
    finally:
        part.unlink(missing_ok=True)


def pick_path(target: Path, rel: Path, f: dict, state: dict) -> Path:
    """Zielpfad bestimmen; bei Namenskollision mit anderer Datei ID anhängen."""
    name = safe_name(f["name"])
    path = target / rel / name
    taken = {v["path"] for k, v in state.items() if k != f["id"]}
    if str(path.relative_to(target)) in taken:
        p = Path(name)
        path = target / rel / f"{p.stem}_{f['id'][:8]}{p.suffix}"
    return path


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    cfg = load_config()
    cfg["target_dir"].mkdir(parents=True, exist_ok=True)

    state = {}
    if cfg["state_file"].exists():
        state = json.loads(cfg["state_file"].read_text())

    creds = Credentials.from_service_account_file(str(cfg["key_file"]), scopes=SCOPES)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    new = errors = 0
    try:
        for f, rel in walk(service, cfg["folder_id"]):
            version = f.get("md5Checksum") or f["modifiedTime"]
            known = state.get(f["id"])
            if known and known["version"] == version:
                local = cfg["target_dir"] / known["path"]
                if local.exists():
                    continue  # unverändert und noch vorhanden
            dest = pick_path(cfg["target_dir"], rel, f, state)
            try:
                download(service, f["id"], dest)
            except (HttpError, OSError) as e:
                errors += 1
                log.error("Fehler bei %s: %s", f["name"], e)
                continue
            state[f["id"]] = {
                "path": str(dest.relative_to(cfg["target_dir"])),
                "version": version,
            }
            # State nach jeder Datei sichern, falls der Lauf abbricht
            cfg["state_file"].write_text(json.dumps(state, indent=2))
            new += 1
            log.info("Gesichert: %s", dest.relative_to(cfg["target_dir"]))
    except HttpError as e:
        log.error("Drive-API-Fehler: %s", e)
        return 2

    log.info("Fertig: %d neu/aktualisiert, %d Fehler", new, errors)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
