"""Source normalization: search results and fetched pages -> ``Source`` records (plan.md §17).

Credibility is assigned by cheap static rules on domain and path (specs.md §7.2, §26.4's
hybrid option). A URL no rule recognises becomes ``other``/``low`` with
``confident=False``, so the ambiguous cases can later be sent to a cheap LLM
classification once the LLM gateway exists. Low is the safe default: a low-tier source
can show pain or sentiment but never alone support a quantitative fact.
"""

from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

from opp.domain.base import utcnow
from opp.domain.enums import Level, SourceType
from opp.domain.evidence import Source
from opp.research.extractor import ExtractedPage
from opp.research.search import SearchResult

EXCERPT_CHARS = 500


@dataclass(frozen=True)
class Classification:
    source_type: SourceType
    credibility_tier: Level
    confident: bool = True


_GOV_SUFFIXES = (
    ".gov",
    ".mil",
    ".gov.uk",
    ".gov.au",
    ".gc.ca",
    ".europa.eu",
    ".gouv.fr",
    ".bund.de",
    ".gov.in",
    ".govt.nz",
)

_DOMAINS: dict[str, Classification] = {}


def _register(kind: SourceType, tier: Level, *domains: str) -> None:
    for domain in domains:
        _DOMAINS[domain] = Classification(kind, tier)


_register(
    SourceType.INDUSTRY_REPORT,
    Level.HIGH,
    "statista.com",
    "gartner.com",
    "forrester.com",
    "mckinsey.com",
    "ibisworld.com",
    "pewresearch.org",
    "worldbank.org",
    "oecd.org",
    "imf.org",
)
_register(
    SourceType.TRADE_PRESS,
    Level.MEDIUM,
    "reuters.com",
    "apnews.com",
    "bloomberg.com",
    "wsj.com",
    "ft.com",
    "economist.com",
    "nytimes.com",
    "cnbc.com",
    "forbes.com",
    "businessinsider.com",
    "techcrunch.com",
    "theverge.com",
    "axios.com",
)
_register(
    SourceType.REVIEW,
    Level.MEDIUM,
    "g2.com",
    "capterra.com",
    "getapp.com",
    "softwareadvice.com",
    "trustpilot.com",
    "yelp.com",
    "tripadvisor.com",
    "producthunt.com",
)
_register(
    SourceType.JOB_POST,
    Level.MEDIUM,
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "monster.com",
    "lever.co",
    "greenhouse.io",
    "workable.com",
)
_register(
    SourceType.FORUM,
    Level.LOW,
    "reddit.com",
    "news.ycombinator.com",
    "quora.com",
    "stackexchange.com",
    "stackoverflow.com",
    "twitter.com",
    "x.com",
    "facebook.com",
)
_register(
    SourceType.OTHER,
    Level.LOW,
    "medium.com",
    "substack.com",
    "blogspot.com",
    "wordpress.com",
    "tumblr.com",
)

_FORUM_PREFIXES = ("forum.", "forums.", "community.", "discuss.")
_PRICING_SEGMENTS = frozenset({"pricing", "plans", "price", "prices"})
_DOCS_SEGMENTS = frozenset({"docs", "documentation", "help", "support", "kb"})


def classify(url: str) -> Classification:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    segments = {s.lower() for s in parts.path.split("/") if s}

    if host.endswith(_GOV_SUFFIXES) or host in {s.lstrip(".") for s in _GOV_SUFFIXES}:
        return Classification(SourceType.REGULATORY, Level.HIGH)
    if host == "linkedin.com" and "jobs" in segments:
        return Classification(SourceType.JOB_POST, Level.MEDIUM)
    for domain, classification in _DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return classification
    if host.startswith(_FORUM_PREFIXES):
        return Classification(SourceType.FORUM, Level.LOW)
    if host.startswith("blog.") or "blog" in segments:
        return Classification(SourceType.OTHER, Level.LOW)
    if segments & _PRICING_SEGMENTS:
        return Classification(SourceType.PRICING_PAGE, Level.HIGH)
    if host.startswith("docs.") or segments & _DOCS_SEGMENTS:
        return Classification(SourceType.VENDOR, Level.MEDIUM)
    return Classification(SourceType.OTHER, Level.LOW, confident=False)


def source_from_search_result(result: SearchResult, *, accessed: date | None = None) -> Source:
    """A snippet-backed Source: the snippet is the excerpt a discovery claim rests on."""
    classification = classify(result.url)
    return Source(
        url=result.url,
        title=result.title or _host(result.url),
        source_type=classification.source_type,
        credibility_tier=classification.credibility_tier,
        published_date=result.published_date,
        excerpt=_excerpt(result.snippet),
        accessed_date=accessed or utcnow().date(),
    )


def source_from_page(
    page: ExtractedPage, *, excerpt: str | None = None, accessed: date | None = None
) -> Source:
    """A Source for a fetched page. Pass the passage a claim rests on as ``excerpt``;
    otherwise the start of the page text is used."""
    classification = classify(page.url)
    text = excerpt if excerpt and excerpt.strip() else page.text or page.title
    if not text.strip():
        raise ValueError(f"page {page.url} has no text to use as an excerpt")
    return Source(
        url=page.url,
        title=page.title or _host(page.url),
        source_type=classification.source_type,
        credibility_tier=classification.credibility_tier,
        publisher=page.publisher,
        published_date=page.published_date,
        excerpt=_excerpt(text),
        accessed_date=accessed or utcnow().date(),
    )


def _excerpt(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= EXCERPT_CHARS else text[: EXCERPT_CHARS - 1].rstrip() + "…"


def _host(url: str) -> str:
    return (urlsplit(url).hostname or url).removeprefix("www.")
