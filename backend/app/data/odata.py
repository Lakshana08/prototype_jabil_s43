"""Minimal SAP Gateway OData V4 client (stdlib only).

Reads entity sets from the S43 service ``zsd_smc_material`` (see ``SMC_OData_V4_URLs.xlsx`` and the
registry in ``app.data.s4_apis``). Records are returned as the service sends them, minus the
``@odata.*`` annotations: expanded navigation properties are plain lists (``_ComponentShortage: [...]``),
numbers are numbers and dates are ``YYYY-MM-DD`` strings. Server-side paging (``@odata.nextLink``)
is followed until the requested rows are read.
"""
from __future__ import annotations

import base64
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

# S43 pages by 100 rows unless the request carries $top; with $top it sends up to 5000 per page.
# A "read everything" request therefore asks for this many rows and follows @odata.nextLink.
READ_ALL_TOP = 1_000_000


class ODataError(RuntimeError):
    pass


class ODataClient:
    def __init__(self, base_url: str, client: str, user: str = "", password: str = "",
                 verify_ssl: bool | str = True, timeout: float = 60.0, destination=None):
        self.base_url = base_url.rstrip("/")
        # Optional app.data.destination.Destination: adds its auth / connectivity-proxy headers
        # and routes requests through the BTP Connectivity proxy (OnPremise destinations).
        self.destination = destination
        self.client = client
        self.timeout = timeout
        self._headers = {"Accept": "application/json"}
        if user:
            token = base64.b64encode(f"{user}:{password}".encode()).decode()
            self._headers["Authorization"] = f"Basic {token}"
        if verify_ssl is False:
            ssl_context = ssl._create_unverified_context()  # noqa: S323 - opt-in via SAP_VERIFY_SSL=false
        elif isinstance(verify_ssl, str):
            ssl_context = ssl.create_default_context(cafile=verify_ssl)
        else:
            ssl_context = ssl.create_default_context()
        handlers: list = [urllib.request.HTTPSHandler(context=ssl_context)]
        if destination is not None and destination.proxies():
            handlers.insert(0, urllib.request.ProxyHandler(destination.proxies()))
        self._opener = urllib.request.build_opener(*handlers)

    @property
    def service_name(self) -> str:
        """``zsd_smc_material (OData V4)`` - for provenance texts."""
        parts = [p for p in self.base_url.split("/") if p]
        name = parts[-2] if len(parts) >= 2 and parts[-1].isdigit() else parts[-1] if parts else self.base_url
        return f"{name} (OData V4)"

    # ------------------------------------------------------------------ public
    def url(self, entity_set: str, expand: list[str] | None = None, filter: str | None = None,
            top: int | None = None, orderby: str | None = None, select: list[str] | None = None) -> str:
        params = {"sap-client": self.client, "$count": "true"}
        if expand:
            params["$expand"] = ",".join(expand)
        if filter:
            params["$filter"] = filter
        if top is not None:
            params["$top"] = str(top)
        if orderby:
            params["$orderby"] = orderby
        if select:
            params["$select"] = ",".join(select)
        return f"{self.base_url}/{entity_set}?{urllib.parse.urlencode(params, safe='$,_', quote_via=urllib.parse.quote)}"

    def read(self, entity_set: str, expand: list[str] | None = None, filter: str | None = None,
             top: int | None = None, orderby: str | None = None, select: list[str] | None = None) -> list[dict]:
        """GET the records of an entity set that match ``filter`` (server-side).

        ``top`` caps the number of records; without it every matching record is read.
        """
        url: str | None = self.url(entity_set, expand, filter, READ_ALL_TOP if top is None else top, orderby, select)
        results: list[dict] = []
        while url:
            payload = self._get_json(url)
            try:
                page, nxt = payload["value"], payload.get("@odata.nextLink")
            except (KeyError, TypeError) as exc:
                raise ODataError(f"{entity_set}: not an OData V4 'value' payload ({exc})") from exc
            results.extend(_clean(rec) for rec in page)
            url = self._absolute(nxt) if nxt and (top is None or len(results) < top) else None
        return results[:top] if top is not None else results

    # ------------------------------------------------------------------ internals
    def _get_json(self, url: str) -> dict:
        headers = dict(self._headers)
        if self.destination is not None:
            try:
                headers.update(self.destination.request_headers())
            except Exception as exc:  # noqa: BLE001 - surface as a data-load error
                raise ODataError(f"BTP destination {self.destination.name!r}: {exc}") from exc
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ODataError(f"HTTP {exc.code} for {_redact(url)}: check SAP_USER / SAP_PASSWORD and the "
                                 "user's authorization for the S43 OData service") from exc
            body = exc.read()[:500].decode("utf-8", "replace")
            raise ODataError(f"HTTP {exc.code} for {_redact(url)}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
            hint = ""
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                hint = " (set SAP_VERIFY_SSL to the S43 CA bundle path, or 'false' for dev only)"
            raise ODataError(f"Cannot reach {_redact(url)}: {exc}{hint}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ODataError(f"Non-JSON response from {_redact(url)}: {raw[:200]!r}") from exc

    def _absolute(self, link: str) -> str:
        url = link if link.startswith("http") else urllib.parse.urljoin(self.base_url + "/", link)
        if "sap-client=" not in url:  # SAP's next-page link does not always carry sap-client
            url += ("&" if "?" in url else "?") + f"sap-client={self.client}"
        return url


def _clean(rec: dict) -> dict:
    """Drop ``@odata.*`` annotations, also inside expanded navigation properties."""
    out: dict = {}
    for key, value in rec.items():
        if "@odata." in key:
            continue
        if isinstance(value, list):
            value = [_clean(v) if isinstance(v, dict) else v for v in value]
        elif isinstance(value, dict):
            value = _clean(value)
        out[key] = value
    return out


def _redact(url: str) -> str:
    return url.split("?", 1)[0]


# ---------------------------------------------------------------------- $filter helpers
_SAFE_VALUE = re.compile(r"^[\w .\-/+:]{1,60}$")


def literal(value) -> str:
    """OData string literal for a user-supplied key. Rejects anything that is not a plain SAP key."""
    text = str(value).strip()
    if not _SAFE_VALUE.match(text):
        raise ODataError(f"Not a valid key: {text[:40]!r}")
    return "'" + text.replace("'", "''") + "'"


def eq(field: str, value) -> str:
    return f"{field} eq {literal(value)}"


def any_of(field: str, values) -> str:
    """``(F eq 'a' or F eq 'b')``."""
    parts = [eq(field, v) for v in values]
    return parts[0] if len(parts) == 1 else "(" + " or ".join(parts) + ")"


def all_of(*clauses: str | None) -> str | None:
    clauses = [c for c in clauses if c]
    return " and ".join(clauses) or None
