import pytest

from opp.evidence.urls import canonicalize_url, url_hash, url_host


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("https://Example.com/Pricing/", "https://example.com/Pricing"),
        ("https://www.example.com/a", "https://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com/a?utm_source=x&b=2&a=1&gclid=z", "https://example.com/a?a=1&b=2"),
        ("https://example.com", "https://example.com/"),
        ("  https://example.com/a  ", "https://example.com/a"),
    ],
)
def test_canonicalize_url(raw: str, canonical: str) -> None:
    assert canonicalize_url(raw) == canonical


def test_variants_of_one_page_share_a_hash() -> None:
    assert url_hash("https://www.example.com/p/?utm_medium=email#top") == url_hash(
        "https://example.com/p"
    )


def test_different_pages_have_different_hashes() -> None:
    assert url_hash("https://example.com/a") != url_hash("https://example.com/b")
    assert url_hash("https://example.com/a?id=1") != url_hash("https://example.com/a?id=2")


def test_url_host() -> None:
    assert url_host("https://www.Reddit.com/r/physio/comments/1") == "reddit.com"
