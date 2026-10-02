"""AgentCore Platform v1.0 - Notion REST API v1 client.

Service layer: a thin wrapper around the Notion REST API v1 pages / blocks /
search endpoints. Contains NO business logic, NO routing, and NO credentials -
the integration token is passed in per call by the node (which reads it via
ctx.secrets). This module imports no framework/SDK internals - pure stdlib
(import-isolation, PB-4).

Transports:
    ``NotionClient.live()`` wires a stdlib ``http.client`` transport that
    performs real Notion REST API v1 calls - this is what both entry points
    (``cli.py`` Marketplace Pod, ``src/api/server.py`` standalone) inject.
    With no transport injected, a deterministic NETWORK-FREE stub returns the
    documented Notion response shape (a synthetic page ``id`` + ``url`` marked
    ``_stub: True``) so tests and the STG mock deploy run without a workspace.
"""

from __future__ import annotations

import hashlib
import json
import http.client
import socket
import ssl
import urllib.parse
import urllib.request
from typing import Any, Callable

# A transport callable: (url, headers, json_body) -> (status_code, response_dict)
Transport = Callable[[str, dict[str, str], dict[str, Any]], "tuple[int, dict[str, Any]]"]

_NOTION_VERSION = "2022-06-28"
_BASE_URL = "https://api.notion.com"
# Socket timeout for the live transport. The node's deadline fails the call
# closed, but a hung socket would still pin its worker thread and block the
# one-shot Pod from exiting - so the socket itself must time out too.
_HTTP_TIMEOUT_S = 30.0


def _open(host: str) -> http.client.HTTPSConnection:
    """HTTPS connection to ``host``, tunnelled through ``$HTTPS_PROXY`` when set.

    http.client's own tunnel sends ``CONNECT ... HTTP/1.0`` (hardcoded through
    Python 3.11), which the Marketplace Pod's Envoy egress sidecar rejects with
    426 - so the CONNECT is sent here as HTTP/1.1.
    """
    conn = http.client.HTTPSConnection(host, timeout=_HTTP_TIMEOUT_S)
    proxy = urllib.request.getproxies().get("https")
    if not proxy or urllib.request.proxy_bypass(host):
        return conn
    # ponytail: no proxy auth (user:pass@) - neither the Pod sidecar nor CI needs it.
    p = urllib.parse.urlsplit(proxy)
    sock = socket.create_connection((p.hostname or "", p.port or 80), timeout=_HTTP_TIMEOUT_S)
    sock.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode("ascii"))
    resp = http.client.HTTPResponse(sock, method="CONNECT")
    resp.begin()
    if resp.status != 200:
        sock.close()
        raise OSError(f"Tunnel connection failed: {resp.status}")
    conn.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    return conn


def _http_transport(method: str) -> Transport:
    """Build a live stdlib transport for one HTTP method.

    Non-2xx responses are returned as (status, body) - never raised - so the
    client's own NotionApiError path owns error handling. The call deadline is
    enforced by the calling node (CallNotionApiNode._with_deadline).
    """

    def send(url: str, headers: dict[str, str], json_body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        parts = urllib.parse.urlsplit(url)
        conn = _open(parts.hostname or "")
        try:
            body = json.dumps(json_body).encode("utf-8") if json_body is not None else None
            conn.request(method, parts.path + (f"?{parts.query}" if parts.query else ""), body=body, headers=headers)
            resp = conn.getresponse()
            status, raw = resp.status, resp.read()
        finally:
            conn.close()
        try:
            return status, json.loads(raw or b"{}")
        except ValueError:
            return status, {}

    return send


class NotionApiError(Exception):
    """Raised when the Notion REST API returns a non-2xx status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Notion API error {status_code}: {message}")


class NotionClient:
    """Notion REST API v1 pages / blocks / search client.

    Args:
        base_url: Notion API base URL (default https://api.notion.com).
        post/patch/get: optional injected transports (tests or a live client).
            When none is injected, a deterministic NETWORK-FREE v1 stub is used
            (see the module docstring - it returns the documented shape without
            a live Notion write).
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        *,
        post: Transport | None = None,
        patch: Transport | None = None,
        get: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._post = post
        self._patch = patch
        self._get = get

    @classmethod
    def live(cls, base_url: str = _BASE_URL) -> NotionClient:
        """A client that performs real Notion REST API v1 writes (stdlib http.client)."""
        return cls(base_url, post=_http_transport("POST"), patch=_http_transport("PATCH"))

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_token: str) -> dict[str, str]:
        """Build the Notion REST API v1 auth + version headers.

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "Notion-Version": _NOTION_VERSION,
        }

    # -- v1 deterministic stub transport (default; NO network) ----------------

    def _stub_transport(
        self, url: str, headers: dict[str, str], json_body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Deterministic, network-free v1 stub - returns the documented Notion shape.

        NOT a live call. The synthetic id/url are derived from the request so the
        response is stable and inspectable. See the module docstring for the v1
        limitation and how to inject a live transport.
        """
        seed = url + "|" + json.dumps(json_body, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        page_id = "-".join((digest[0:8], digest[8:12], digest[12:16], digest[16:20], digest[20:32]))
        if url.endswith("/v1/search"):
            return 200, {"object": "list", "results": [], "next_cursor": None, "has_more": False}
        if "/v1/blocks/" in url:
            return 200, {"object": "list", "type": "block", "results": []}
        # /v1/pages create - return the documented page shape.
        return 200, {
            "object": "page",
            "id": page_id,
            "url": f"https://www.notion.so/{page_id.replace('-', '')}",
            "properties": (json_body or {}).get("properties", {}),
            "_stub": True,  # marks the network-free v1 stub response
        }

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    # -- public API ---------------------------------------------------------

    def create_page(self, payload: dict[str, Any], api_token: str) -> dict[str, Any]:
        """POST /v1/pages - create a page or a database record.

        Returns the parsed response dict (containing at least ``id`` and ``url``).
        Raises NotionApiError on a non-2xx status.
        """
        url = f"{self._base_url}/v1/pages"
        transport = self._resolve(self._post)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise NotionApiError(status, _err_message(body))
        return body

    def append_blocks(self, block_id: str, children: list[dict[str, Any]], api_token: str) -> dict[str, Any]:
        """PATCH /v1/blocks/{block_id}/children - append blocks to an existing page.

        Raises NotionApiError on a non-2xx status.
        """
        url = f"{self._base_url}/v1/blocks/{block_id}/children"
        transport = self._resolve(self._patch)
        status, body = transport(url, self._headers(api_token), {"children": children})
        if not (200 <= status < 300):
            raise NotionApiError(status, _err_message(body))
        return body

    def search(self, query: str, filter_value: str, api_token: str) -> dict[str, Any]:
        """POST /v1/search - resolve a page/database name to Notion objects.

        filter_value is "page" or "database". Raises NotionApiError on non-2xx.
        """
        url = f"{self._base_url}/v1/search"
        transport = self._resolve(self._post)
        payload = {"query": query, "filter": {"value": filter_value, "property": "object"}}
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise NotionApiError(status, _err_message(body))
        return body


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from a Notion error body."""
    if isinstance(body, dict):
        msg = body.get("message")
        if msg:
            return str(msg)
        code = body.get("code")
        if code:
            return str(code)
    return str(body)
