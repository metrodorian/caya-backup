# caya-drive-sync

Zieht die von Caya in Google Drive abgelegten PDFs per Drive-API auf den NAS-Pi.
Reines Backup: Drive wird nur gelesen (`drive.readonly`), lokal wird nie gelöscht.

## Einrichtung

### 0. Caya mit Google Drive verbinden
In Caya unter **Integrationen → Google Drive** verbinden und einen festen Zielordner
wählen (z. B. `Caya`). Diesen Ordner sichert das Skript.

### 1. Google-Cloud-Projekt anlegen, Drive API aktivieren
1. <https://console.cloud.google.com> öffnen, mit dem Google-Konto anmelden,
   dem der Drive gehört.
2. Oben links auf die Projektauswahl → **Neues Projekt** → Name z. B.
   `caya-backup` → **Erstellen**. Danach das neue Projekt auswählen.
3. Menü → **APIs & Dienste → Bibliothek** → nach „Google Drive API" suchen →
   **Aktivieren**.

### 2. Service-Konto erstellen, JSON-Schlüssel herunterladen
1. Menü → **IAM & Verwaltung → Dienstkonten** → **Dienstkonto erstellen**.
2. Name z. B. `caya-sync` → **Erstellen und fortfahren**. Rollen und
   Nutzerzugriff leer lassen → **Fertig**.
3. Das neue Dienstkonto anklicken → Reiter **Schlüssel** → **Schlüssel
   hinzufügen → Neuen Schlüssel erstellen** → **JSON** → **Erstellen**.
   Die Datei wird heruntergeladen.
4. Die Adresse des Dienstkontos notieren, sie sieht so aus:
   `caya-sync@<projekt-id>.iam.gserviceaccount.com`.

> Der Schlüssel ist ein Passwort. Nicht ins Repo, nicht per Mail verschicken,
> nur auf den Pi kopieren (`chmod 600`). Lässt sich unter **Schlüssel**
> jederzeit löschen und neu erzeugen.

### 3. Caya-Ordner mit dem Service-Konto teilen
1. In <https://drive.google.com> Rechtsklick auf den Caya-Ordner → **Teilen**.
2. Die Dienstkonto-Adresse aus Schritt 2 eintragen, Rolle **Betrachter**.
3. „Personen benachrichtigen" abhaken → **Senden**.

### 4. Ordner-ID ermitteln
Den Ordner in Drive öffnen. Die URL lautet
`https://drive.google.com/drive/folders/<ORDNER-ID>` – der Teil nach
`/folders/` (ohne `?…`) kommt als `folder_id` in `config.json`.

### 2. Auf dem Pi
```bash
sudo mkdir -p /opt/caya-drive-sync && sudo chown $USER /opt/caya-drive-sync
# Dateien hierher kopieren: sync.py, requirements.txt, config.json, service-account.json
cd /opt/caya-drive-sync
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
chmod 600 service-account.json config.json
cp config.example.json config.json   # dann anpassen
.venv/bin/python sync.py             # Testlauf
```

### 3. Cronjob (täglich 02:00)
```
0 2 * * * /opt/caya-drive-sync/.venv/bin/python /opt/caya-drive-sync/sync.py >> /var/log/caya-sync.log 2>&1
```

## Verhalten
- Rekursiv über Unterordner, Ordnerstruktur wird gespiegelt.
- Änderungserkennung per MD5 (Fallback: `modifiedTime`); Status in `state.json`.
- Download nach `.part`, dann atomares Umbenennen.
- Namenskollisionen: Datei-ID wird an den Namen angehängt.
- Google-eigene Formate (Docs/Sheets) werden übersprungen.
- Exit-Code 0 = ok, 1 = einzelne Dateien fehlgeschlagen, 2 = API-Fehler.
