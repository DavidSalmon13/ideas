from datetime import date

from opp.research.extractor import extract
from opp.research.fetcher import RawPage

ARTICLE = """<html><head>
<title>HVAC contractors still book by phone | Trade Weekly</title>
<meta property="og:site_name" content="Trade Weekly">
<meta property="article:published_time" content="2026-05-04T09:00:00Z">
</head><body>
<nav><a href="/">Home</a> <a href="/about">About</a> Subscribe now!</nav>
<article><h1>HVAC contractors still book by phone</h1>
<p>A survey of 400 independent HVAC contractors found that most still schedule
technicians by phone and spreadsheet, and lose several hours a week to double bookings.</p>
<p>Owners said switching tools is hard because their dispatchers are not technical,
and existing field-service suites are priced for larger firms.</p>
</article>
<footer>Copyright Trade Weekly. Cookie settings.</footer>
</body></html>"""


def _raw(text: str, content_type: str = "text/html") -> RawPage:
    return RawPage(
        url="https://trade.example.com/hvac",
        final_url="https://trade.example.com/hvac",
        status_code=200,
        content_type=content_type,
        text=text,
    )


def test_extracts_main_text_and_metadata_from_html() -> None:
    page = extract(_raw(ARTICLE))

    assert page.url == "https://trade.example.com/hvac"
    assert "HVAC contractors still book by phone" in page.title
    assert "400 independent HVAC contractors" in page.text
    assert "Subscribe now" not in page.text
    assert "Cookie settings" not in page.text
    assert page.published_date == date(2026, 5, 4)
    assert page.publisher == "Trade Weekly"
    assert not page.is_empty


def test_plain_text_passes_through() -> None:
    page = extract(_raw("  just some text \n", content_type="text/plain"))
    assert page.text == "just some text"
    assert page.title == ""


def test_page_without_content_is_empty() -> None:
    assert extract(_raw("<html><body></body></html>")).is_empty
