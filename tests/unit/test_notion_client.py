# CMN-C2-237 - Unit tests: NotionClient service (Notion REST API v1 shape)
# Mirrors the sibling ToolCallingAgent's tests/unit/test_jira_client.py (Jira -> Notion).

import pytest

from src.services.notion_client import NotionApiError, NotionClient


def test_create_page_success():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, {"object": "page", "id": "page-1", "url": "https://www.notion.so/page1"}

    client = NotionClient("https://api.notion.com/", post=post)
    resp = client.create_page({"parent": {"page_id": "x"}, "properties": {}}, "tok123")
    assert resp["id"] == "page-1"
    assert captured["url"] == "https://api.notion.com/v1/pages"
    # Notion v1 auth is a Bearer token + the pinned Notion-Version header.
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["headers"]["Notion-Version"] == "2022-06-28"


def test_append_blocks_success():
    captured = {}

    def patch(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"object": "list", "type": "block", "results": []}

    client = NotionClient("https://api.notion.com", patch=patch)
    resp = client.append_blocks("blk-1", [{"object": "block"}], "tok")
    assert resp["object"] == "list"
    assert captured["url"] == "https://api.notion.com/v1/blocks/blk-1/children"
    assert captured["body"] == {"children": [{"object": "block"}]}


def test_search_success():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"object": "list", "results": [], "has_more": False}

    client = NotionClient("https://api.notion.com", post=post)
    resp = client.search("Home", "page", "tok")
    assert resp["object"] == "list"
    assert captured["url"] == "https://api.notion.com/v1/search"
    assert captured["body"]["query"] == "Home"
    assert captured["body"]["filter"] == {"value": "page", "property": "object"}


def test_create_page_error_raises():
    def post(url, headers, body):
        return 400, {"message": "body failed validation"}

    client = NotionClient("https://api.notion.com", post=post)
    with pytest.raises(NotionApiError) as exc:
        client.create_page({"parent": {"page_id": "x"}, "properties": {}}, "tok")
    assert exc.value.status_code == 400
    assert "body failed validation" in str(exc.value)


def test_default_stub_transport_returns_page_shape():
    # No transport injected -> deterministic, network-free v1 stub.
    client = NotionClient()
    resp = client.create_page({"parent": {"page_id": "abc"}, "properties": {}}, "tok")
    assert resp["object"] == "page"
    assert resp["id"]
    assert resp["url"].startswith("https://www.notion.so/")
    assert resp.get("_stub") is True


def test_live_client_performs_real_http_calls(monkeypatch):
    # live() must wire a real transport - never the stub - and map an HTTP
    # error response onto NotionApiError via the client's own status check.
    import http.client

    import src.services.notion_client as nc

    calls = []

    class _Resp:
        def __init__(self, status, raw):
            self.status, self._raw = status, raw

        def read(self):
            return self._raw

    class _Conn:
        def __init__(self, host, timeout=None):
            assert timeout, "live transport must set a socket timeout"
            self.host = host

        def request(self, method, path, body=None, headers=None):
            calls.append((method, self.host, path))
            self._path = path

        def getresponse(self):
            if self._path.endswith("/children"):
                return _Resp(404, b'{"message": "not shared"}')
            return _Resp(200, b'{"object": "page", "id": "real-1", "url": "https://www.notion.so/real1"}')

        def close(self):
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", _Conn)
    monkeypatch.setattr(nc.urllib.request, "getproxies", lambda: {})
    client = NotionClient.live()
    resp = client.create_page({"parent": {"page_id": "x"}, "properties": {}}, "tok")
    assert resp["id"] == "real-1" and "_stub" not in resp
    with pytest.raises(NotionApiError) as exc:
        client.append_blocks("blk-1", [], "tok")
    assert exc.value.status_code == 404 and "not shared" in str(exc.value)
    assert calls == [
        ("POST", "api.notion.com", "/v1/pages"),
        ("PATCH", "api.notion.com", "/v1/blocks/blk-1/children"),
    ]


def test_proxy_tunnel_uses_http11_connect(monkeypatch):
    # Regression: Python 3.11's http.client tunnel sends "CONNECT ... HTTP/1.0",
    # which the Marketplace Envoy egress sidecar rejects with 426. The transport
    # must send HTTP/1.1 and surface a refused tunnel as an error, not a write.
    import socket
    import threading

    import src.services.notion_client as nc

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    seen = {}

    def proxy():
        c, _ = srv.accept()
        seen["line"] = c.recv(4096).split(b"\r\n")[0]
        c.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        c.close()

    t = threading.Thread(target=proxy, daemon=True)
    t.start()
    port = srv.getsockname()[1]
    monkeypatch.setattr(nc.urllib.request, "getproxies", lambda: {"https": f"http://127.0.0.1:{port}"})
    monkeypatch.setattr(nc.urllib.request, "proxy_bypass", lambda host: False)
    with pytest.raises(OSError, match="Tunnel connection failed: 403"):
        nc._open("api.notion.com")
    t.join(5)
    srv.close()
    assert seen["line"] == b"CONNECT api.notion.com:443 HTTP/1.1"
