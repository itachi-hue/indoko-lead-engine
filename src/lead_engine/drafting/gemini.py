"""Gemini API client (paid tier). Returns parsed JSON from structured-output requests."""
from __future__ import annotations

import json

import httpx

from .. import http

BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiError(Exception):
    pass


class GeminiClient:
    def __init__(self, client: httpx.Client, api_key: str, model: str = ""):
        self.client, self.api_key, self.model = client, api_key, model

    def _h(self) -> dict:
        if not self.api_key:
            raise GeminiError("GEMINI_API_KEY is not set")
        return {"x-goog-api-key": self.api_key}

    def ping(self) -> dict:
        try:
            data = http.get_json(self.client, f"{BASE}/models?pageSize=50", headers=self._h())
            names = [m.get("name", "").removeprefix("models/") for m in data.get("models", [])]
            return {"ok": True, "model_available": (self.model in names) if self.model else None,
                    "models": names[:10]}
        except http.HTTPError as e:
            return {"ok": False, "status": e.status, "detail": e.body[:200]}

    def generate_json(self, system: str, prompt: str, schema: dict, temperature: float = 0.6) -> dict:
        """One structured generation. Raises GeminiError on a block or unparseable output."""
        if not self.model:
            raise GeminiError("gemini.model is not set in config.toml")
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "responseMimeType": "application/json",
                                 "responseSchema": schema},
        }
        data = http.post_json(self.client, f"{BASE}/models/{self.model}:generateContent", body,
                              headers=self._h())
        cands = data.get("candidates") or []
        if not cands:
            raise GeminiError(f"no candidates (blocked: {data.get('promptFeedback', {})})")
        cand = cands[0]
        if cand.get("finishReason") not in (None, "STOP", "MAX_TOKENS"):
            raise GeminiError(f"generation stopped: {cand.get('finishReason')}")
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise GeminiError("output was not valid JSON") from exc
