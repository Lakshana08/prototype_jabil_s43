"""Minimal SAP AI Core (generative AI hub) chat client - stdlib only.

Calls an Azure OpenAI-compatible foundation-model deployment (e.g. GPT-4o) at
``{AICORE_BASE_URL}/inference/deployments/{LLM_DEPLOYMENT_ID}/chat/completions``
with an OAuth client-credentials token from the AI Core service key.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

API_VERSION = "2024-10-21"
_EXPIRY_MARGIN_S = 120


class LLMError(RuntimeError):
    pass


class AICoreChat:
    def __init__(self, auth_url: str, client_id: str, client_secret: str, base_url: str,
                 deployment_id: str, resource_group: str = "default", timeout: float = 90.0):
        auth_url = auth_url.rstrip("/")
        self._auth_url = auth_url if auth_url.endswith("/oauth/token") else auth_url + "/oauth/token"
        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._url = (f"{base_url.rstrip('/')}/inference/deployments/{deployment_id}"
                     f"/chat/completions?api-version={API_VERSION}")
        self._resource_group = resource_group
        self._timeout = timeout
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.0, max_tokens: int = 1500) -> dict:
        """One chat-completions call; returns the assistant message dict."""
        body: dict = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        payload = self._post(body)
        try:
            return payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected AI Core response: {str(payload)[:300]}") from exc

    # ------------------------------------------------------------------ internals
    def _post(self, body: dict, attempt: int = 0) -> dict:
        req = urllib.request.Request(self._url, data=json.dumps(body).encode(), method="POST", headers={
            "Authorization": f"Bearer {self._access_token()}",
            "AI-Resource-Group": self._resource_group,
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", "replace")
            if exc.code == 401 and attempt == 0:
                self._token = None  # token revoked/expired early - fetch a new one once
                return self._post(body, attempt + 1)
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                return self._post(body, attempt + 1)
            raise LLMError(f"AI Core HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError(f"AI Core call failed: {exc}") from exc

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - _EXPIRY_MARGIN_S:
                return self._token
            req = urllib.request.Request(
                self._auth_url, data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
                method="POST", headers={"Authorization": f"Basic {self._basic}",
                                        "Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    payload = json.loads(resp.read())
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                raise LLMError(f"AI Core token request failed: {exc}") from exc
            self._token = payload["access_token"]
            self._expires_at = time.time() + float(payload.get("expires_in", 3600))
            return self._token
