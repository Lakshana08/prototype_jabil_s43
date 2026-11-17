"""SAP BTP Destination + Connectivity service support (stdlib only).

On Cloud Foundry the S43 system (192.168.8.69, on-premise) is not reachable directly. The
app instead reads a BTP *destination* (default name ``S43``) from the bound Destination
service and, for ``ProxyType=OnPremise``, tunnels every request through the bound
Connectivity service proxy to the SAP Cloud Connector, which forwards it to S43.

Bound service credentials are taken from ``VCAP_SERVICES`` (labels ``destination`` and
``connectivity``). OAuth tokens and the destination lookup are cached and refreshed
before they expire, so the periodic data refresh keeps working for the app's lifetime.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_EXPIRY_MARGIN_S = 120          # refresh tokens this long before they expire
_DESTINATION_CACHE_S = 300      # re-read the destination config (rotated passwords etc.)


class DestinationError(RuntimeError):
    pass


def _vcap_credentials(label: str) -> dict | None:
    """Credentials of the first bound service instance with this label (or instance name)."""
    raw = os.getenv("VCAP_SERVICES")
    if not raw:
        return None
    try:
        services = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DestinationError(f"VCAP_SERVICES is not valid JSON: {exc}") from exc
    for key, instances in services.items():
        for inst in instances:
            if key == label or inst.get("label") == label or label in (inst.get("tags") or []):
                return inst.get("credentials") or {}
    return None


class _ClientCredentialsToken:
    """Cached OAuth2 client-credentials token from an XSUAA token endpoint."""

    def __init__(self, token_url: str, client_id: str, client_secret: str, timeout: float):
        token_url = token_url.rstrip("/")
        self._url = token_url if token_url.endswith("/oauth/token") else token_url + "/oauth/token"
        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._timeout = timeout
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - _EXPIRY_MARGIN_S:
                return self._token
            body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
            req = urllib.request.Request(self._url, data=body, method="POST", headers={
                "Authorization": f"Basic {self._basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    payload = json.loads(resp.read())
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                raise DestinationError(f"OAuth token request to {self._url} failed: {exc}") from exc
            self._token = payload["access_token"]
            self._expires_at = time.time() + float(payload.get("expires_in", 3600))
            return self._token


class Destination:
    """Resolves a named BTP destination into URL, auth headers and (optional) proxy."""

    def __init__(self, name: str, timeout: float = 60.0):
        self.name = name
        self._timeout = timeout
        dest_creds = _vcap_credentials("destination")
        if dest_creds is None:
            raise DestinationError(
                f"SAP_DESTINATION_NAME={name!r} is set but no Destination service is bound "
                "(VCAP_SERVICES has no 'destination' entry) - see manifest.yaml")
        self._service_uri = dest_creds["uri"].rstrip("/")
        self._dest_token = _ClientCredentialsToken(
            dest_creds["url"], dest_creds["clientid"], dest_creds["clientsecret"], timeout)

        conn = _vcap_credentials("connectivity")
        self._conn_token = None
        self._proxy = None
        if conn:
            port = conn.get("onpremise_proxy_http_port") or conn.get("onpremise_proxy_port")
            self._proxy = f"http://{conn['onpremise_proxy_host']}:{port}"
            self._conn_token = _ClientCredentialsToken(
                conn.get("token_service_url") or conn["url"], conn["clientid"], conn["clientsecret"], timeout)

        self._config: dict = {}
        self._auth_headers: dict[str, str] = {}
        self._fetched_at = 0.0
        self._lock = threading.Lock()
        self._refresh(force=True)

    # ------------------------------------------------------------------ public
    @property
    def url(self) -> str:
        return self._config["URL"].rstrip("/")

    @property
    def config(self) -> dict:
        """Raw destination configuration (e.g. ``sap-client``)."""
        return dict(self._config)

    @property
    def is_on_premise(self) -> bool:
        return self._config.get("ProxyType", "Internet").lower() == "onpremise"

    def request_headers(self) -> dict[str, str]:
        """Headers to add to every request sent to the target system."""
        self._refresh()
        headers = dict(self._auth_headers)
        if self.is_on_premise:
            headers["Proxy-Authorization"] = f"Bearer {self._conn_token.get()}"
            location = self._config.get("CloudConnectorLocationId")
            if location:
                headers["SAP-Connectivity-SCC-Location_ID"] = location
        return headers

    def proxies(self) -> dict[str, str]:
        """urllib ProxyHandler mapping (empty for Internet destinations)."""
        return {"http": self._proxy} if self.is_on_premise else {}

    # ------------------------------------------------------------------ internals
    def _refresh(self, force: bool = False) -> None:
        with self._lock:
            if not force and time.time() - self._fetched_at < _DESTINATION_CACHE_S:
                return
            url = f"{self._service_uri}/destination-configuration/v1/destinations/{urllib.parse.quote(self.name)}"
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self._dest_token.get()}", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    payload = json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise DestinationError(
                        f"Destination {self.name!r} not found in the subaccount or service instance") from exc
                raise DestinationError(f"Destination service HTTP {exc.code} for {self.name!r}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                raise DestinationError(f"Destination service lookup for {self.name!r} failed: {exc}") from exc

            config = payload.get("destinationConfiguration") or {}
            if not config.get("URL"):
                raise DestinationError(f"Destination {self.name!r} has no URL")
            headers: dict[str, str] = {}
            for tok in payload.get("authTokens") or []:
                if tok.get("error"):
                    raise DestinationError(f"Destination {self.name!r} auth token error: {tok['error']}")
                hh = tok.get("http_header") or {}
                if hh.get("key"):
                    headers[hh["key"]] = hh["value"]
            if config.get("ProxyType", "").lower() == "onpremise" and not self._proxy:
                raise DestinationError(
                    f"Destination {self.name!r} is OnPremise but no Connectivity service is bound")
            self._config, self._auth_headers, self._fetched_at = config, headers, time.time()
            log.info("Resolved BTP destination %s -> %s (%s, %s)", self.name, config["URL"],
                     config.get("ProxyType", "Internet"), config.get("Authentication", "NoAuthentication"))
