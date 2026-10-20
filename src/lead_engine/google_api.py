"""Minimal Google Sheets and Drive REST clients authenticated as a service account.

Uses google-auth only to sign the JWT; the token exchange and API calls go through the shared
httpx client so they share the retry policy and can be faked in tests.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path

import httpx

from . import http

SCOPES = ("https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.file",
          "https://www.googleapis.com/auth/drive.readonly")
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GSHEET_MIME = "application/vnd.google-apps.spreadsheet"


class ServiceAccountAuth:
    def __init__(self, key_file: str | Path | None, client: httpx.Client, scopes=SCOPES, info: dict | None = None):
        self.info = info if info is not None else json.loads(Path(key_file).read_text())
        self.client = client
        self.scopes = scopes
        self._token: str | None = None
        self._expires = 0.0

    @classmethod
    def from_info(cls, info: dict, client: httpx.Client, scopes=SCOPES) -> "ServiceAccountAuth":
        return cls(None, client, scopes, info=info)

    def _assertion(self) -> str:
        from google.auth import crypt, jwt  # local import keeps tests light

        signer = crypt.RSASigner.from_service_account_info(self.info)
        now = int(time.time())
        payload = {"iss": self.info["client_email"], "scope": " ".join(self.scopes),
                   "aud": self.info.get("token_uri", "https://oauth2.googleapis.com/token"),
                   "iat": now, "exp": now + 3600}
        return jwt.encode(signer, payload).decode()

    def token(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        resp = http.request(self.client, "POST",
                            self.info.get("token_uri", "https://oauth2.googleapis.com/token"),
                            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                  "assertion": self._assertion()})
        body = resp.json()
        self._token = body["access_token"]
        self._expires = time.time() + int(body.get("expires_in", 3600))
        return self._token

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}"}


class StaticAuth:
    """For tests and dry runs."""

    def __init__(self, token: str = "test-token"):
        self._t = token

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self._t}"}


def _q(rng: str) -> str:
    return urllib.parse.quote(rng, safe="")


class Sheets:
    """Spreadsheet values API. Writes are batched to stay under quota."""

    def __init__(self, client: httpx.Client, auth, spreadsheet_id: str):
        self.client, self.auth, self.sid = client, auth, spreadsheet_id

    def _url(self, suffix: str = "") -> str:
        return f"{SHEETS}/{self.sid}{suffix}"

    def metadata(self) -> dict:
        return http.get_json(self.client, self._url("?fields=properties.title,sheets.properties"),
                             headers=self.auth.headers())

    def tab_ids(self) -> dict[str, int]:
        meta = self.metadata()
        return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}

    def ensure_tabs(self, titles: list[str]) -> None:
        existing = self.tab_ids()
        reqs = [{"addSheet": {"properties": {"title": t}}} for t in titles if t not in existing]
        if reqs:
            self.batch_update(reqs)

    def batch_update(self, requests: list[dict]) -> dict:
        return http.post_json(self.client, self._url(":batchUpdate"), {"requests": requests},
                              headers=self.auth.headers())

    def get_values(self, tab: str) -> list[list[str]]:
        data = http.get_json(self.client, self._url(f"/values/{_q(tab)}"), headers=self.auth.headers())
        return data.get("values", [])

    def update_values(self, rng: str, rows: list[list]) -> None:
        http.request(self.client, "PUT", self._url(f"/values/{_q(rng)}?valueInputOption=RAW"),
                     json={"values": rows}, headers=self.auth.headers())

    def batch_update_values(self, data: list[tuple[str, list[list]]]) -> None:
        if not data:
            return
        payload = {"valueInputOption": "RAW",
                   "data": [{"range": rng, "values": rows} for rng, rows in data]}
        http.post_json(self.client, self._url("/values:batchUpdate"), payload,
                       headers=self.auth.headers())

    def append_rows(self, tab: str, rows: list[list]) -> None:
        if not rows:
            return
        http.request(self.client, "POST",
                     self._url(f"/values/{_q(tab)}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"),
                     json={"values": rows}, headers=self.auth.headers())


class Drive:
    def __init__(self, client: httpx.Client, auth):
        self.client, self.auth = client, auth

    def get_meta(self, file_id: str) -> dict:
        return http.get_json(self.client,
                             f"{DRIVE}/{file_id}?fields=id,name,mimeType,modifiedTime&supportsAllDrives=true",
                             headers=self.auth.headers())

    def download_xlsx(self, file_id: str) -> bytes:
        """Excel files download as-is; native Google Sheets are exported as xlsx."""
        meta = self.get_meta(file_id)
        if meta.get("mimeType") == GSHEET_MIME:
            url = f"{DRIVE}/{file_id}/export?mimeType={urllib.parse.quote(XLSX_MIME)}"
        else:
            url = f"{DRIVE}/{file_id}?alt=media&supportsAllDrives=true"
        return http.request(self.client, "GET", url, headers=self.auth.headers()).content

    def upload(self, folder_id: str, name: str, content: bytes, mime: str = "application/octet-stream") -> str:
        boundary = "leadengineboundary"
        meta = json.dumps({"name": name, "parents": [folder_id]})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + content + \
            f"\r\n--{boundary}--".encode()
        resp = http.request(self.client, "POST", f"{UPLOAD}?uploadType=multipart&supportsAllDrives=true",
                            content=body,
                            headers={**self.auth.headers(),
                                     "Content-Type": f"multipart/related; boundary={boundary}"})
        return resp.json()["id"]

    def list_folder(self, folder_id: str) -> list[dict]:
        q = urllib.parse.quote(f"'{folder_id}' in parents and trashed=false")
        data = http.get_json(self.client, f"{DRIVE}?q={q}&fields=files(id,name,createdTime)"
                             "&supportsAllDrives=true&includeItemsFromAllDrives=true",
                             headers=self.auth.headers())
        return data.get("files", [])

    def delete(self, file_id: str) -> None:
        http.request(self.client, "DELETE", f"{DRIVE}/{file_id}?supportsAllDrives=true",
                     headers=self.auth.headers())
