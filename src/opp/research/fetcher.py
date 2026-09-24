"""The SSRF-guarded Fetcher (plan.md §17-§18). Every URL an agent fetches is hostile input.

Guards, applied to the first request and to every redirect hop:

- only ``http``/``https``, no credentials in the URL, only common web ports;
- the host is resolved *here*, and every address must be globally routable (no private,
  loopback, link-local, CGNAT, multicast or reserved ranges, IPv4-mapped IPv6 included);
- the connection then goes to that checked IP, with the original ``Host`` header and TLS
  server name, so DNS can't be swapped between the check and the connect (rebinding);
- redirects are followed by hand, at most ``max_redirects`` hops, each one re-checked;
- only text content types are read, the body is streamed and capped at ``max_bytes``,
  and the whole call has a timeout.

Keep-alive reuse is off, because pooled connections are keyed by the pinned IP and a
TLS session verified for one hostname must not carry another's request. Environment
proxies are ignored on purpose: a proxy would do its own DNS and defeat the
check. Failures raise ``FetchError`` (or ``BlockedURLError`` for a guard hit); callers
degrade to ``unknown`` rather than guessing.
"""

import asyncio
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import UUID

import httpx
from pydantic import Field

from opp.domain.base import DomainModel
from opp.domain.enums import StageName, TraceKind
from opp.domain.report import Trace
from opp.research.search import TraceSink

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str], Awaitable[list[IPAddress]]]

ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
TEXT_CONTENT_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "text/plain", "application/xml", "text/xml"}
)
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
USER_AGENT = "opp-research/0.1 (+personal research tool)"


class RawPage(DomainModel):
    url: str
    final_url: str
    status_code: int
    content_type: str
    text: str
    truncated: bool = False
    size_bytes: int = Field(default=0, ge=0)


class FetchError(Exception):
    def __init__(self, message: str, *, url: str) -> None:
        super().__init__(message)
        self.url = url


class BlockedURLError(FetchError):
    """The URL or one of its redirects failed an SSRF guard."""


@runtime_checkable
class PageFetcher(Protocol):
    async def fetch(self, url: str) -> RawPage: ...


async def system_resolver(host: str) -> list[IPAddress]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def is_public_address(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class Fetcher:
    def __init__(
        self,
        *,
        timeout_s: float = 15.0,
        max_bytes: int = 2_000_000,
        max_redirects: int = 5,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._resolver = resolver
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout_s,
            follow_redirects=False,
            trust_env=False,
            # Connections are keyed by IP after pinning; never reuse one across hostnames.
            limits=httpx.Limits(max_keepalive_connections=0),
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"},
        )

    async def fetch(self, url: str) -> RawPage:
        try:
            return await asyncio.wait_for(self._fetch(url), timeout=self._timeout_s)
        except TimeoutError as exc:
            raise FetchError("timed out", url=url) from exc

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _fetch(self, url: str) -> RawPage:
        current = url
        for _ in range(self._max_redirects + 1):
            request = await self._pinned_request(current)
            try:
                response = await self._client.send(request, stream=True)
            except httpx.HTTPError as exc:
                raise FetchError(f"request failed: {type(exc).__name__}", url=current) from exc
            try:
                if response.status_code in _REDIRECTS:
                    location = response.headers.get("Location")
                    if not location:
                        raise FetchError("redirect without Location", url=current)
                    current = urljoin(current, location)
                    continue
                return await self._read(url, current, response)
            finally:
                await response.aclose()
        raise FetchError(f"more than {self._max_redirects} redirects", url=url)

    async def _pinned_request(self, url: str) -> httpx.Request:
        """Check the URL and build a request that connects to the vetted IP."""
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise BlockedURLError(f"scheme {scheme!r} not allowed", url=url)
        if parts.username or parts.password:
            raise BlockedURLError("credentials in URL not allowed", url=url)
        host = parts.hostname
        if not host:
            raise BlockedURLError("URL has no host", url=url)
        try:
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise BlockedURLError("invalid port", url=url) from exc
        if port not in ALLOWED_PORTS:
            raise BlockedURLError(f"port {port} not allowed", url=url)

        ip = await self._vetted_ip(host, url)
        ip_host = f"[{ip}]" if ip.version == 6 else str(ip)
        default_port = port == (443 if scheme == "https" else 80)
        netloc = ip_host if default_port else f"{ip_host}:{port}"
        host_header = host if default_port else f"{host}:{port}"
        pinned = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
        return self._client.build_request(
            "GET",
            pinned,
            headers={"Host": host_header},
            extensions={"sni_hostname": host} if scheme == "https" else {},
        )

    async def _vetted_ip(self, host: str, url: str) -> IPAddress:
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                addresses = await self._resolver(host)
            except OSError as exc:
                raise FetchError(f"could not resolve {host}", url=url) from exc
        if not addresses:
            raise FetchError(f"could not resolve {host}", url=url)
        # Every address must be public: one private record is enough to refuse.
        for address in addresses:
            if not is_public_address(address):
                raise BlockedURLError(f"{host} resolves to non-public {address}", url=url)
        return addresses[0]

    async def _read(self, url: str, final_url: str, response: httpx.Response) -> RawPage:
        content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if response.is_error:
            raise FetchError(f"HTTP {response.status_code}", url=final_url)
        if content_type not in TEXT_CONTENT_TYPES:
            raise FetchError(f"unsupported content type {content_type or 'none'!r}", url=final_url)
        declared = response.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > self._max_bytes:
            raise FetchError(f"body larger than {self._max_bytes} bytes", url=final_url)

        body = bytearray()
        truncated = False
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > self._max_bytes:
                del body[self._max_bytes :]
                truncated = True
                break
        encoding = response.encoding or "utf-8"
        return RawPage(
            url=url,
            final_url=final_url,
            status_code=response.status_code,
            content_type=content_type,
            text=bytes(body).decode(encoding, errors="replace"),
            truncated=truncated,
            size_bytes=len(body),
        )


class MeteredFetcher:
    """Records a Trace per fetch (fetches are free but rate- and budget-limited)."""

    def __init__(
        self,
        fetcher: PageFetcher,
        sink: TraceSink,
        *,
        run_id: UUID,
        stage: StageName | None = None,
        agent: str | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._sink = sink
        self._run_id = run_id
        self._stage = stage
        self._agent = agent

    async def fetch(self, url: str) -> RawPage:
        started = time.perf_counter()
        try:
            page = await self._fetcher.fetch(url)
        except FetchError as exc:
            self._record(started, ok=False, error=str(exc))
            raise
        self._record(started, ok=True)
        return page

    def _record(self, started: float, *, ok: bool, error: str | None = None) -> None:
        self._sink(
            Trace(
                run_id=self._run_id,
                stage=self._stage,
                agent=self._agent,
                kind=TraceKind.FETCH,
                latency_ms=int((time.perf_counter() - started) * 1000),
                ok=ok,
                error=error,
            )
        )
