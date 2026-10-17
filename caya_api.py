"""Schlanker Client für Cayas GraphQL-Schnittstelle (nur lesende Abfragen).

Anmeldung über AWS Cognito: Das Refresh-Token stammt aus der Browser-Sitzung
von caya_login.py (.auth/state.json) und wird in .auth/tokens.json gehalten.
"""
import json
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
AUTH = BASE / ".auth"
STATE = AUTH / "state.json"
TOKENS = AUTH / "tokens.json"

API = "https://customer-api.caya.com/"
COGNITO = "https://cognito-idp.eu-central-1.amazonaws.com/"
CLIENT_ID = "smi9060mf6k8oee4ainard6g5"

Q_ROOTS = """query Roots { meFolders { inbox { id } archive { id } } }"""

Q_FOLDERS = """
query Folders($where: ContainerWhereInput!, $cursor: String, $count: Int) {
  connection: containersConnection(where: $where, first: $count, after: $cursor, orderBy: title_ASC) {
    edges { node { ... on ContainerFolder { id title } } }
    pageInfo { endCursor hasNextPage }
  }
}"""

Q_DOCUMENTS = """
query Documents($after: String, $first: Int, $where: ContainerDocumentsByFolderWhereInput!) {
  connection: getContainerDocumentsByFolderConnection(
    after: $after, first: $first, orderBy: createdAt_DESC, where: $where) {
    edges { node { ... on ContainerDocument {
      id filename fileType createdAt file canDownload isDestroyed
      metadata { senderName subject tags }
      scanMetadata { date }
    } } }
    pageInfo { endCursor hasNextPage }
    aggregate { count }
  }
}"""


class SessionExpired(Exception):
    pass


class Caya:
    def __init__(self):
        self.http = requests.Session()
        self._id_token = None
        self._expires = 0.0

    # --- Anmeldung -------------------------------------------------------
    def _refresh_token(self) -> str:
        if TOKENS.exists():
            return json.loads(TOKENS.read_text())["refresh_token"]
        if not STATE.exists():
            raise SessionExpired("Keine Sitzung – erst caya_login.py ausführen")
        state = json.loads(STATE.read_text())
        for origin in state["origins"]:
            for item in origin["localStorage"]:
                if item["name"].endswith(".refreshToken"):
                    self._save_refresh(item["value"])
                    return item["value"]
        raise SessionExpired("Kein Refresh-Token in der Sitzung – caya_login.py ausführen")

    @staticmethod
    def _save_refresh(token: str):
        AUTH.mkdir(mode=0o700, exist_ok=True)
        TOKENS.write_text(json.dumps({"refresh_token": token}))
        TOKENS.chmod(0o600)

    def _ensure_token(self):
        if self._id_token and time.time() < self._expires - 120:
            return
        r = self.http.post(COGNITO, timeout=30, headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        }, json={"AuthFlow": "REFRESH_TOKEN_AUTH", "ClientId": CLIENT_ID,
                 "AuthParameters": {"REFRESH_TOKEN": self._refresh_token()}})
        if r.status_code != 200:
            TOKENS.unlink(missing_ok=True)
            raise SessionExpired(f"Token-Erneuerung fehlgeschlagen ({r.status_code}) – "
                                 "caya_login.py ausführen")
        res = r.json()["AuthenticationResult"]
        self._id_token = res["IdToken"]
        self._expires = time.time() + res["ExpiresIn"]
        if "RefreshToken" in res:  # bei Token-Rotation das neue merken
            self._save_refresh(res["RefreshToken"])

    def query(self, q: str, variables: dict | None = None) -> dict:
        for attempt in range(4):
            self._ensure_token()
            try:
                r = self.http.post(API, timeout=60, headers={"authorization": self._id_token},
                                   json={"query": q, "variables": variables or {}})
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 401:
                self._id_token = None
                continue
            if r.status_code >= 500 or r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            body = r.json()
            if body.get("errors"):
                raise RuntimeError(f"GraphQL-Fehler: {body['errors'][0].get('message')}")
            return body["data"]
        raise RuntimeError("Caya-API nicht erreichbar")

    # --- Abfragen --------------------------------------------------------
    def roots(self) -> dict:
        f = self.query(Q_ROOTS)["meFolders"]
        return {"Inbox": f["inbox"]["id"], "Ablage": f["archive"]["id"]}

    def subfolders(self, folder_id: str):
        cursor = None
        while True:
            c = self.query(Q_FOLDERS, {"count": 1000, "cursor": cursor, "where": {
                "folder": {"id": folder_id}, "type": "Folder"}})["connection"]
            for e in c["edges"]:
                if e["node"]:
                    yield e["node"]
            if not c["pageInfo"]["hasNextPage"]:
                return
            cursor = c["pageInfo"]["endCursor"]

    def documents(self, folder_id: str, page_size: int = 50):
        """Liefert Dokumente seitenweise; die Download-Links sind nur kurz gültig."""
        after = None
        while True:
            c = self.query(Q_DOCUMENTS, {"first": page_size, "after": after, "where": {
                "folder": {"id": folder_id},
                "type_in": ["DocumentDigital", "DocumentScan"]}})["connection"]
            yield [e["node"] for e in c["edges"] if e["node"]]
            if not c["pageInfo"]["hasNextPage"]:
                return
            after = c["pageInfo"]["endCursor"]

    def download(self, url: str, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        try:
            with self.http.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with part.open("wb") as fh:
                    for chunk in r.iter_content(1 << 20):
                        fh.write(chunk)
            part.replace(dest)
        finally:
            part.unlink(missing_ok=True)
