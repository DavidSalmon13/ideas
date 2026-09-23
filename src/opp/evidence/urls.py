"""URL canonicalisation, so one page is one Source however its link was written."""

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = frozenset(
    {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "ref", "ref_src", "igshid"}
)
_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonicalize_url(url: str) -> str:
    """Lowercase scheme/host, drop `www.`, default ports, fragments and tracking
    parameters, sort the remaining query and trim a trailing slash."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.port is not None and parts.port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/") or "/"
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_tracking(key)
        )
    )
    return urlunsplit((scheme, host, path, query, ""))


def url_hash(url: str) -> str:
    """Dedup key for a Source: a digest of the canonical URL."""
    return hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()


def url_host(url: str) -> str:
    """The host a source was published on, used to judge whether sources are independent."""
    return (urlsplit(url.strip()).hostname or "").lower().removeprefix("www.")


def _is_tracking(key: str) -> bool:
    key = key.lower()
    return key.startswith("utm_") or key in _TRACKING_PARAMS
