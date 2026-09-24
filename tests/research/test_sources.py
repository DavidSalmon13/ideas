from datetime import date

import pytest

from opp.domain.enums import Level, SourceType
from opp.research.extractor import ExtractedPage
from opp.research.search import SearchResult
from opp.research.sources import (
    EXCERPT_CHARS,
    classify,
    source_from_page,
    source_from_search_result,
)


@pytest.mark.parametrize(
    ("url", "kind", "tier"),
    [
        ("https://www.sec.gov/rules/final/2026", SourceType.REGULATORY, Level.HIGH),
        ("https://www.epa.gov/section608", SourceType.REGULATORY, Level.HIGH),
        ("https://www.legislation.gov.uk/ukpga/2026/1", SourceType.REGULATORY, Level.HIGH),
        ("https://eur-lex.europa.eu/eli/reg/2026/1", SourceType.REGULATORY, Level.HIGH),
        ("https://www.statista.com/statistics/1", SourceType.INDUSTRY_REPORT, Level.HIGH),
        ("https://www.reuters.com/business/x", SourceType.TRADE_PRESS, Level.MEDIUM),
        ("https://www.g2.com/products/x/reviews", SourceType.REVIEW, Level.MEDIUM),
        ("https://jobs.lever.co/acme/123", SourceType.JOB_POST, Level.MEDIUM),
        ("https://www.linkedin.com/jobs/view/123", SourceType.JOB_POST, Level.MEDIUM),
        ("https://old.reddit.com/r/HVAC/comments/1", SourceType.FORUM, Level.LOW),
        ("https://news.ycombinator.com/item?id=1", SourceType.FORUM, Level.LOW),
        ("https://community.acme.com/t/1", SourceType.FORUM, Level.LOW),
        ("https://someone.substack.com/p/x", SourceType.OTHER, Level.LOW),
        ("https://acme.com/blog/launch", SourceType.OTHER, Level.LOW),
        ("https://acme.com/pricing", SourceType.PRICING_PAGE, Level.HIGH),
        ("https://docs.acme.com/api", SourceType.VENDOR, Level.MEDIUM),
        ("https://acme.com/help/billing", SourceType.VENDOR, Level.MEDIUM),
    ],
)
def test_classify_known_patterns(url: str, kind: SourceType, tier: Level) -> None:
    result = classify(url)
    assert (result.source_type, result.credibility_tier, result.confident) == (kind, tier, True)


def test_unknown_sites_default_to_low_and_are_marked_ambiguous() -> None:
    result = classify("https://random-site.io/article/7")
    assert (result.source_type, result.credibility_tier) == (SourceType.OTHER, Level.LOW)
    assert not result.confident


def test_lookalike_domains_do_not_match() -> None:
    assert classify("https://notreddit.com/r/x").source_type is SourceType.OTHER
    assert classify("https://gov.evil.com/").source_type is SourceType.OTHER


def test_source_from_search_result_uses_the_snippet_as_excerpt() -> None:
    result = SearchResult(
        title="",
        url="https://www.reddit.com/r/HVAC/comments/1",
        snippet="  Scheduling   is a nightmare\nfor small shops ",
        published_date=date(2026, 6, 1),
    )

    source = source_from_search_result(result, accessed=date(2026, 9, 24))

    assert source.excerpt == "Scheduling is a nightmare for small shops"
    assert source.title == "reddit.com"
    assert (source.source_type, source.credibility_tier) == (SourceType.FORUM, Level.LOW)
    assert (source.published_date, source.accessed_date) == (date(2026, 6, 1), date(2026, 9, 24))


def test_source_from_page_prefers_the_given_excerpt_and_truncates() -> None:
    page = ExtractedPage(
        url="https://acme.com/pricing",
        title="Pricing",
        text="word " * 400,
        publisher="Acme",
    )

    quoted = source_from_page(page, excerpt="Pro plan: $49/month")
    default = source_from_page(page)

    assert quoted.excerpt == "Pro plan: $49/month"
    assert quoted.publisher == "Acme"
    assert quoted.source_type is SourceType.PRICING_PAGE
    assert len(default.excerpt) == EXCERPT_CHARS
    assert default.excerpt.endswith("…")


def test_source_from_empty_page_is_refused() -> None:
    with pytest.raises(ValueError):
        source_from_page(ExtractedPage(url="https://acme.com/x"))
