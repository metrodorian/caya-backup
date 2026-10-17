# caya-backup

Sichert die Briefpost aus [Caya](https://www.caya.com) lokal und sortiert neue
Dokumente in einen bestehenden Dokumente-Ordner ein.

Caya hat keine offizielle API. Das Projekt nutzt die GraphQL-Schnittstelle,
mit der auch Cayas Web-App arbeitet – **ausschließlich lesend**. In Caya wird
nichts verändert. Ändert Caya die Schnittstelle, muss der Client angepasst werden.

## Ablauf

| Schritt | Skript | Was passiert |
|---|---|---|
| Anmeldung | `caya_login.py` | Meldet sich mit den Daten aus `.env` an und speichert die Sitzung in `.auth/` |
| Abruf | `caya_fetch.py` | Lädt alle Dokumente aus Inbox und Ablage (mit Unterordnern) nach `downloads/` |
| Abgleich | `caya_compare.py` | Sucht jedes Dokument im lokalen Ordner – per Datei-Hash und Inhalts-Hash |
| Import | `caya_import.py` | Legt fehlende Dokumente als `JJJJ.MM.TT Absender - Betreff.pdf` in den zugeordneten Ordner |
| Wochenlauf | `caya_sync.sh` | Abruf + Import, meldet das Ergebnis an einen Statusmonitor |

### Warum ein Inhalts-Hash?
Caya liefert ältere Dokumente heute mit geänderter PDF-Hülle aus: Seiten und
Bilder sind identisch, die Bytes nicht. Der Inhalts-Hash bildet sich nur aus
Seiteninhalten und eingebetteten Bildern und erkennt solche Kopien trotzdem.

### Regeln des Imports
- Schon vorhandene Dokumente werden nur vermerkt, nie angefasst.
- Es wird nie etwas überschrieben oder gelöscht. Namenskollisionen bekommen `(2)`.
- Wechselt ein vom Sync abgelegtes Dokument in Caya den Ordner, zieht die lokale
  Datei mit – nur wenn sie unverändert am vermerkten Ort liegt.
- Ordnerzuordnung in `zuordnung.csv` (`caya_ordner;lokaler_ordner`). Unbekannte
  Unterordner erben vom Elternordner, sonst landen sie unter `Caya/…`.
- Ohne `--apply` ist der Import ein Trockenlauf (`downloads/import-plan.csv`).

## Einrichtung

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
cp .env.example .env && chmod 600 .env    # Zugangsdaten eintragen
.venv/bin/python caya_login.py --show     # erste Anmeldung, sichtbar
.venv/bin/python caya_fetch.py
.venv/bin/python caya_compare.py          # erzeugt den Zuordnungsvorschlag
.venv/bin/python caya_import.py           # Trockenlauf prüfen, dann --apply
```

Wöchentlich per launchd (`~/Library/LaunchAgents/de.lenny.caya-sync.plist`),
das `caya_sync.sh` aufruft. Läuft die Sitzung ab, meldet sich der Lauf mit den
Daten aus `.env` selbst neu an.

## Privates bleibt lokal
`.env`, `.auth/`, `downloads/` und `zuordnung.csv` sind in `.gitignore` – das
Repo ist öffentlich.

## Altlast
`sync.py` ist die frühere Google-Drive-Variante. Sie wird nicht mehr genutzt,
weil Caya keine Google-Drive-Integration anbietet.
