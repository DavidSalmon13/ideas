import asyncio
import ipaddress
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

from opp.domain.enums import TraceKind
from opp.domain.report import Trace
from opp.research.fetcher import (
    BlockedURLError,
    Fetcher,
    FetchError,
    IPAddress,
    MeteredFetcher,
    PageFetcher,
    RawPage,
    is_public_address,
)

PUBLIC = "93.184.216.34"
HTML = {"Content-Type": "text/html; charset=utf-8"}

Handler = Callable[[httpx.Request], httpx.Response]


def resolver(table: dict[str, list[str]]) -> Callable[[str], Any]:
    async def resolve(host: str) -> list[IPAddress]:
        if host not in table:
            raise OSError("no such host")
        return [ipaddress.ip_address(ip) for ip in table[host]]

    return resolve


class Recorder:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def fetcher(handler: Handler, hosts: dict[str, list[str]] | None = None, **kwargs: Any) -> Fetcher:
    table = {"example.com": [PUBLIC], "other.example.org": [PUBLIC]}
    table.update(hosts or {})
    return Fetcher(transport=httpx.MockTransport(handler), resolver=resolver(table), **kwargs)


def fetch(f: Fetcher, url: str) -> RawPage:
    return asyncio.run(f.fetch(url))


def page(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers=HTML, text="<html><body>hello</body></html>")


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.5",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
    ],
)
def test_non_public_addresses_are_rejected(ip: str) -> None:
    assert not is_public_address(ipaddress.ip_address(ip))


def test_public_addresses_are_allowed() -> None:
    assert is_public_address(ipaddress.ip_address(PUBLIC))
    assert is_public_address(ipaddress.ip_address("2606:4700:4700::1111"))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://internal.example.com/",  # DNS points at a private address
        "http://mixed.example.com/",  # one public and one private record
        "file:///etc/passwd",
        "ftp://example.com/file",
        "gopher://example.com/",
        "http://user:pass@example.com/",
        "http://example.com:22/",
        "http://example.com:6379/",
    ],
)
def test_ssrf_targets_are_blocked_before_any_request(url: str) -> None:
    recorder = Recorder(page)
    f = fetcher(
        recorder,
        {"internal.example.com": ["192.168.0.10"], "mixed.example.com": [PUBLIC, "10.0.0.7"]},
    )

    with pytest.raises(BlockedURLError):
        fetch(f, url)
    assert recorder.requests == []


def test_unresolvable_host_is_a_fetch_error() -> None:
    with pytest.raises(FetchError, match="could not resolve"):
        fetch(fetcher(page), "https://nowhere.invalid/")


def test_connects_to_the_vetted_ip_with_original_host_and_sni() -> None:
    recorder = Recorder(page)

    result = fetch(fetcher(recorder), "https://example.com/pricing?plan=pro#top")

    [request] = recorder.requests
    assert request.url.host == PUBLIC
    assert request.url.path == "/pricing"
    assert request.url.query == b"plan=pro"
    assert request.headers["Host"] == "example.com"
    assert request.extensions["sni_hostname"] == "example.com"
    assert (result.final_url, result.text) == (
        "https://example.com/pricing?plan=pro#top",
        "<html><body>hello</body></html>",
    )


def test_non_default_port_is_kept_in_host_header() -> None:
    recorder = Recorder(page)
    fetch(fetcher(recorder), "http://example.com:8080/x")
    [request] = recorder.requests
    assert (request.url.port, request.headers["Host"]) == (8080, "example.com:8080")
    assert "sni_hostname" not in request.extensions


def test_follows_redirects_and_rechecks_each_hop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Host"] == "example.com":
            return httpx.Response(301, headers={"Location": "https://other.example.org/final"})
        if request.url.path == "/final":
            return httpx.Response(302, headers={"Location": "/landing"})
        return page(request)

    recorder = Recorder(handler)
    result = fetch(fetcher(recorder), "http://example.com/start")

    assert result.url == "http://example.com/start"
    assert result.final_url == "https://other.example.org/landing"
    assert [r.headers["Host"] for r in recorder.requests] == [
        "example.com",
        "other.example.org",
        "other.example.org",
    ]


def test_redirect_to_a_private_address_is_blocked() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"})

    recorder = Recorder(handler)
    with pytest.raises(BlockedURLError):
        fetch(fetcher(recorder), "https://example.com/")
    assert len(recorder.requests) == 1


def test_redirect_loops_are_capped() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/again"})

    recorder = Recorder(handler)
    with pytest.raises(FetchError, match="redirects"):
        fetch(fetcher(recorder, max_redirects=3), "https://example.com/")
    assert len(recorder.requests) == 4


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(404, headers=HTML, text="gone"), "HTTP 404"),
        (
            httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=b"%PDF"),
            "content type",
        ),
        (httpx.Response(200, content=b"no type"), "content type"),
        (httpx.Response(302), "without Location"),
    ],
)
def test_unusable_responses_raise(response: httpx.Response, message: str) -> None:
    with pytest.raises(FetchError, match=message):
        fetch(fetcher(lambda _: response), "https://example.com/")


def test_declared_oversize_body_is_refused() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={**HTML, "Content-Length": "5000"}, content=b"x" * 5000)

    with pytest.raises(FetchError, match="larger than"):
        fetch(fetcher(handler, max_bytes=1000), "https://example.com/")


def test_streamed_body_is_truncated_at_the_cap() -> None:
    async def chunks() -> Any:
        yield b"a" * 600
        yield b"b" * 600

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, content=chunks())

    result = fetch(fetcher(handler, max_bytes=1000), "https://example.com/")

    assert result.truncated
    assert result.size_bytes == 1000
    assert result.text == "a" * 600 + "b" * 400


def test_transport_errors_become_fetch_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(FetchError, match="ConnectError"):
        fetch(fetcher(handler), "https://example.com/")


def test_ignores_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    f = Fetcher()
    assert not f._client._mounts or all(v is None for v in f._client._mounts.values())
    assert isinstance(f, PageFetcher)
    asyncio.run(f.aclose())


def test_metered_fetcher_records_traces() -> None:
    traces: list[Trace] = []
    run_id = uuid4()
    ok = MeteredFetcher(fetcher(page), traces.append, run_id=run_id, agent="specialist")
    bad = MeteredFetcher(fetcher(page), traces.append, run_id=run_id)

    asyncio.run(ok.fetch("https://example.com/"))
    with pytest.raises(BlockedURLError):
        asyncio.run(bad.fetch("http://127.0.0.1/"))

    assert [(t.kind, t.ok, t.agent) for t in traces] == [
        (TraceKind.FETCH, True, "specialist"),
        (TraceKind.FETCH, False, None),
    ]
    assert traces[1].error is not None and "non-public" in traces[1].error
