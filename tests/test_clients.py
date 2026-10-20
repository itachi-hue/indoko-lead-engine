"""Request shapes of the real API clients, checked against the fake HTTP router."""
import json

import httpx
import pytest

from lead_engine import http
from lead_engine.drafting.gemini import GeminiClient, GeminiError
from lead_engine.google_api import Drive, ServiceAccountAuth, Sheets, StaticAuth
from lead_engine.instantly.client import InstantlyClient
from conftest import Router


def client(router):
    return http.make_client(transport=httpx.MockTransport(router))


def test_instantly_add_lead_shape():
    r = Router().add("POST", r"/api/v2/leads$", {"id": "L1"})
    c = InstantlyClient(client(r), "key")
    assert c.add_lead("camp", "a@b.co", "Ada", {"subject": "Hi", "n": 3}) == {"id": "L1"}
    req = r.calls[0]
    body = Router.body(req)
    assert req.headers["authorization"] == "Bearer key"
    assert body["campaign"] == "camp" and body["skip_if_in_workspace"] is True
    assert body["custom_variables"] == {"subject": "Hi", "n": "3"}          # all strings


def test_instantly_pagination():
    pages = [{"items": [{"id": 1}], "next_starting_after": "c1"}, {"items": [{"id": 2}]}]
    r = Router().add("POST", r"/leads/list", handler=lambda req: httpx.Response(200, json=pages.pop(0)))
    assert [x["id"] for x in InstantlyClient(client(r), "k").list_leads("camp")] == [1, 2]
    assert Router.body(r.calls[1])["starting_after"] == "c1"


def test_instantly_missing_key():
    with pytest.raises(Exception, match="INSTANTLY_API_KEY"):
        InstantlyClient(client(Router()), "").add_lead("c", "a@b.co", "A", {})


def test_gemini_structured_output():
    out = {"subject": "s", "body": "b", "fu1": "", "fu2": "", "evidence_ids": []}
    r = Router().add("POST", r":generateContent", {"candidates": [{"finishReason": "STOP",
                     "content": {"parts": [{"text": json.dumps(out)}]}}]})
    g = GeminiClient(client(r), "k", "gemini-x")
    assert g.generate_json("sys", "prompt", {"type": "OBJECT"}) == out
    body = Router.body(r.calls[0])
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert r.calls[0].headers["x-goog-api-key"] == "k"
    assert "models/gemini-x:generateContent" in str(r.calls[0].url)


@pytest.mark.parametrize("payload", [
    {"candidates": []},
    {"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}]},
    {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "not json"}]}}]},
])
def test_gemini_failures_raise(payload):
    r = Router().add("POST", r":generateContent", payload)
    with pytest.raises(GeminiError):
        GeminiClient(client(r), "k", "m").generate_json("s", "p", {})


def test_sheets_append_and_batch_update():
    r = Router().add("POST", r"values/Review:append", {}).add("POST", r"values:batchUpdate", {})
    s = Sheets(client(r), StaticAuth("t"), "sid")
    s.append_rows("Review", [["a", 1]])
    s.batch_update_values([("Review!A2", [["x"]])])
    assert "valueInputOption=RAW" in str(r.calls[0].url)
    assert Router.body(r.calls[1])["data"][0]["range"] == "Review!A2"
    assert r.calls[0].headers["authorization"] == "Bearer t"


def test_drive_exports_native_sheets_and_downloads_xlsx():
    r = Router()
    r.add("GET", r"files/g1\?fields", {"mimeType": "application/vnd.google-apps.spreadsheet"})
    r.add("GET", r"files/g1/export", b"XLSX-EXPORT")
    r.add("GET", r"files/x1\?fields", {"mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})
    r.add("GET", r"files/x1\?alt=media", b"XLSX-FILE")
    d = Drive(client(r), StaticAuth())
    assert d.download_xlsx("g1") == b"XLSX-EXPORT"
    assert d.download_xlsx("x1") == b"XLSX-FILE"


def test_service_account_token_exchange(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    key = tmp_path / "sa.json"
    key.write_text(json.dumps({"client_email": "bot@proj.iam.gserviceaccount.com",
                               "private_key": pem, "private_key_id": "k1",
                               "token_uri": "https://oauth2.googleapis.com/token"}))
    r = Router().add("POST", r"oauth2.googleapis.com/token", {"access_token": "tok", "expires_in": 3600})
    auth = ServiceAccountAuth(key, client(r))
    assert auth.headers() == {"Authorization": "Bearer tok"}
    assert auth.headers() == {"Authorization": "Bearer tok"}
    assert len(r.calls) == 1                                       # token cached
    assert b"jwt-bearer" in r.calls[0].content
