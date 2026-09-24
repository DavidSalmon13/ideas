"""Turns a fetched page into clean text plus metadata (plan.md §17). Pure: no network.

HTML goes through trafilatura (main-content extraction: drops navigation, ads and
boilerplate); plain text is used as-is. The result is still untrusted web content, so it
must only reach a prompt inside a delimited DATA block (plan.md §15, §18).
"""

import json
from datetime import date

import trafilatura

from opp.domain.base import DomainModel
from opp.research.fetcher import RawPage


class ExtractedPage(DomainModel):
    url: str
    title: str = ""
    text: str = ""
    published_date: date | None = None
    publisher: str | None = None
    author: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


def extract(page: RawPage) -> ExtractedPage:
    if page.content_type == "text/plain":
        return ExtractedPage(url=page.final_url, text=page.text.strip())

    raw = trafilatura.extract(
        page.text,
        url=page.final_url,
        output_format="json",
        with_metadata=True,
        include_comments=False,
        include_tables=True,
    )
    if not raw:
        return ExtractedPage(url=page.final_url)
    data = json.loads(raw)
    return ExtractedPage(
        url=page.final_url,
        title=_text(data.get("title")) or "",
        text=_text(data.get("text")) or "",
        published_date=_date(data.get("date")),
        publisher=_text(data.get("source-hostname")) or _text(data.get("sitename")),
        author=_text(data.get("author")),
    )


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None
