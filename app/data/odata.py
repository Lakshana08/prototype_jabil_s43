"""Minimal SAP Gateway OData V2 client (stdlib only).

Reads entity sets from the S43 service ``ZSB_SMC_MATERIAL_V2`` (see
``SMC_All_OData_URLs_Complete 3.xlsx``) and normalises the SAP V2 JSON wire format to
the plain shape the business rules expect (the same shape as the old sample JSON files):

* ``Edm.Decimal`` / ``Edm.Double`` / ``Edm.Int64`` arrive as strings -> converted to numbers
  using the service ``$metadata``.
* ``Edm.DateTime`` / ``Edm.DateTimeOffset`` arrive as ``/Date(ms)/`` -> ISO ``YYYY-MM-DDTHH:MM:SS``.
* ``__metadata`` / ``__deferred`` bookkeeping is removed.
* Server-side paging (``__next``) is followed until the full set is read.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")
_INT_TYPES = {"Edm.Int16", "Edm.Int32", "Edm.Int64", "Edm.Byte", "Edm.SByte"}
_FLOAT_TYPES = {"Edm.Decimal", "Edm.Double", "Edm.Single"}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ODataError(RuntimeError):
    pass


class ODataClient:
    def __init__(self, base_url: str, client: str, user: str = "", password: str = "",
                 verify_ssl: bool | str = True, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.timeout = timeout
        self._headers = {"Accept": "application/json"}
        if user:
            token = base64.b64encode(f"{user}:{password}".encode()).decode()
            self._headers["Authorization"] = f"Basic {token}"
        if verify_ssl is False:
            self._ssl = ssl._create_unverified_context()  # noqa: S323 - opt-in via SAP_VERIFY_SSL=false
        elif isinstance(verify_ssl, str):
            self._ssl = ssl.create_default_context(cafile=verify_ssl)
        else:
            self._ssl = ssl.create_default_context()
        self._types: dict[str, dict[str, str]] | None = None

    # ------------------------------------------------------------------ public
    def url(self, entity_set: str, expand: list[str] | None = None) -> str:
        params = {"sap-client": self.client, "$format": "json", "$inlinecount": "allpages"}
        if expand:
            params["$expand"] = ",".join(expand)
        return f"{self.base_url}/{entity_set}?{urllib.parse.urlencode(params, safe='$,_')}"

    def read_all(self, entity_set: str, expand: list[str] | None = None) -> list[dict]:
        """GET every record of an entity set (following ``__next``), normalised."""
        types = self._property_types()
        url: str | None = self.url(entity_set, expand)
        results: list[dict] = []
        while url:
            payload = self._get_json(url)
            try:
                envelope = payload["d"]
                page = envelope["results"] if isinstance(envelope, dict) and "results" in envelope else envelope
            except (KeyError, TypeError) as exc:
                raise ODataError(f"{entity_set}: not an OData V2 'd.results' payload ({exc})") from exc
            results.extend(_normalise(rec, types) for rec in page)
            nxt = envelope.get("__next") if isinstance(envelope, dict) else None
            url = self._absolute(nxt) if nxt else None
        return results

    # ------------------------------------------------------------------ internals
    def _get(self, url: str, accept: str | None = None) -> bytes:
        headers = dict(self._headers)
        if accept:
            headers["Accept"] = accept
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ODataError(f"HTTP {exc.code} for {_redact(url)}: check SAP_USER / SAP_PASSWORD and the "
                                 "user's authorization for service ZSB_SMC_MATERIAL_V2") from exc
            body = exc.read()[:500].decode("utf-8", "replace")
            raise ODataError(f"HTTP {exc.code} for {_redact(url)}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
            hint = ""
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                hint = " (set SAP_VERIFY_SSL to the S43 CA bundle path, or 'false' for dev only)"
            raise ODataError(f"Cannot reach {_redact(url)}: {exc}{hint}") from exc

    def _get_json(self, url: str) -> dict:
        raw = self._get(url)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ODataError(f"Non-JSON response from {_redact(url)}: {raw[:200]!r}") from exc

    def _absolute(self, link: str) -> str:
        if link.startswith("http"):
            url = link
        else:
            url = urllib.parse.urljoin(self.base_url + "/", link)
        # SAP's __next link does not always carry sap-client / $format.
        if "sap-client=" not in url:
            url += ("&" if "?" in url else "?") + f"sap-client={self.client}"
        if "$format=" not in url:
            url += "&$format=json"
        return url

    def _property_types(self) -> dict[str, dict[str, str]]:
        """{qualified EntityType name: {property: Edm type}} from $metadata (cached)."""
        if self._types is not None:
            return self._types
        types: dict[str, dict[str, str]] = {}
        try:
            xml = self._get(f"{self.base_url}/$metadata?sap-client={self.client}", accept="application/xml")
            root = ET.fromstring(xml)
            for schema in root.iter():
                if not schema.tag.endswith("}Schema"):
                    continue
                ns = schema.get("Namespace", "")
                for et in schema:
                    if et.tag.endswith("}EntityType"):
                        types[f"{ns}.{et.get('Name')}"] = {
                            p.get("Name"): p.get("Type") for p in et if p.tag.endswith("}Property")}
        except (ODataError, ET.ParseError) as exc:
            log.warning("Could not read $metadata (%s); numeric fields will not be type-converted", exc)
        self._types = types
        return types


def _normalise(rec: dict, types: dict[str, dict[str, str]]) -> dict:
    meta = rec.get("__metadata") or {}
    props = types.get(meta.get("type", ""), {})
    out: dict = {}
    for key, value in rec.items():
        if key == "__metadata":
            continue
        if isinstance(value, dict):
            if "__deferred" in value:
                continue  # navigation property that was not $expand-ed
            if "results" in value:
                out[key] = {"results": [_normalise(r, types) for r in value["results"]]}
            else:
                out[key] = _normalise(value, types)  # to-one navigation / complex type
            continue
        out[key] = _convert(value, props.get(key))
    return out


def _convert(value, edm_type: str | None):
    if isinstance(value, str):
        m = _DATE_RE.match(value)
        if m:
            ts = _EPOCH + timedelta(milliseconds=int(m.group(1)))
            return ts.strftime("%Y-%m-%dT%H:%M:%S")
        if edm_type in _FLOAT_TYPES or edm_type in _INT_TYPES:
            text = value.strip()
            if not text:
                return None
            try:
                return int(text) if edm_type in _INT_TYPES else float(text)
            except ValueError:
                return value
    return value


def _redact(url: str) -> str:
    return url.split("?", 1)[0]
