"""Dedup keys: canonical source URLs and lead/opportunity fingerprints (plan.md §14).

Pure functions with no DB access, so the DB layer can import them without a cycle.

- **Sources** dedup on a hash of the *canonical* URL, so tracking parameters, ``www.``,
  ``http`` vs ``https``, fragments and query order don't create separate sources.
- **Leads/opportunities** dedup on a "semantic hash" of customer + pain + business shape:
  the texts are reduced to sorted sets of content words, so rewordings that only change
  case, punctuation, stopwords, plurals or word order hash the same. Looser rewordings
  are caught by :func:`similarity`, a token-overlap score used against a run's own leads.
"""

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from opp.domain.enums import BusinessShape, Level
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Lead
from opp.domain.opportunity import Opportunity

# Bump when the fingerprint recipe changes, so old and new hashes never collide.
FINGERPRINT_VERSION = "v1"

# Both token sets (customer and pain) must overlap at least this much to be a near-duplicate.
NEAR_DUPLICATE_THRESHOLD = 0.6

_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "_ga",
        "_hsenc",
        "_hsmi",
    }
)
_DEFAULT_PORTS = {"http": 80, "https": 443}

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "them",
        "they",
        "this",
        "to",
        "was",
        "were",
        "who",
        "whose",
        "with",
        "without",
        "which",
        "while",
        "will",
        "would",
        "can",
        "could",
        "our",
        "your",
        "you",
        "we",
        "us",
    ]
)
_WORD = re.compile(r"[a-z0-9]+")


def canonical_url(url: str) -> str:
    """Normalise a URL so the same page always maps to the same string."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").rstrip(".")
    host = host.removeprefix("www.")
    port = parts.port
    netloc = host if port is None or port == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"

    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    if len(path) > 1:
        path = path.rstrip("/")

    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(key)
    )
    # http and https serve the same document for dedup purposes.
    return urlunsplit(("https", netloc, path, urlencode(query), ""))


def url_hash(url: str) -> str:
    """Dedup key for a Source: a digest of its canonical URL."""
    return _digest(canonical_url(url))


def _is_tracking(key: str) -> bool:
    key = key.lower()
    return key.startswith("utm_") or key in _TRACKING_PARAMS


def content_tokens(text: str) -> frozenset[str]:
    """Lowercase content words with stopwords dropped and simple plurals folded."""
    return frozenset(_stem(word) for word in _WORD.findall(text.lower()) if word not in _STOPWORDS)


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def fingerprint(customer: str, pain: str, shape: BusinessShape) -> str:
    """The semantic dedup hash of customer + pain + business shape."""
    parts = (
        FINGERPRINT_VERSION,
        shape.value,
        " ".join(sorted(content_tokens(customer))),
        " ".join(sorted(content_tokens(pain))),
    )
    return _digest("|".join(parts))


def lead_dedup_hash(lead: Lead) -> str:
    return fingerprint(
        lead.named_customer.statement, lead.specific_pain.statement, lead.business_shape
    )


def opportunity_dedup_hash(opportunity: Opportunity) -> str | None:
    """The opportunity's fingerprint, or ``None`` until its customer and problem exist."""
    customer = opportunity.customer.segment or opportunity.customer.buyer
    pain = opportunity.problem.description
    if customer is None or pain is None:
        return None
    return fingerprint(customer.statement, pain.statement, opportunity.business_shape)


def similarity(a: Lead, b: Lead) -> float:
    """How alike two leads are, 0-1: the weaker of the customer and pain token overlaps.
    Leads of different business shapes are never similar."""
    if a.business_shape != b.business_shape:
        return 0.0
    return min(
        _jaccard(a.named_customer.statement, b.named_customer.statement),
        _jaccard(a.specific_pain.statement, b.specific_pain.statement),
    )


def _jaccard(a: str, b: str) -> float:
    left, right = content_tokens(a), content_tokens(b)
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


_TIER_WEIGHT = {Level.HIGH: 3, Level.MEDIUM: 2, Level.LOW: 1}


def evidence_score(claims: Iterable[Claim], sources: Mapping[UUID, Source]) -> int:
    """A rough evidence strength used to pick the survivor among duplicates: credibility
    of every resolvable source behind the claims, plus each claim's confidence."""
    score = 0
    for claim in claims:
        score += _TIER_WEIGHT[claim.confidence]
        score += sum(
            _TIER_WEIGHT[sources[sid].credibility_tier]
            for sid in claim.source_ids
            if sid in sources
        )
    return score


@dataclass(frozen=True)
class DuplicatePair:
    keep: Lead
    drop: Lead


def pick_survivor(a: Lead, b: Lead, sources: Mapping[UUID, Source]) -> DuplicatePair:
    """Strongest evidence wins (specs.md §4.5); ties keep the earlier-discovered lead."""
    score_a = evidence_score(a.claims(), sources)
    score_b = evidence_score(b.claims(), sources)
    if score_a > score_b or (score_a == score_b and a.discovered_at <= b.discovered_at):
        return DuplicatePair(keep=a, drop=b)
    return DuplicatePair(keep=b, drop=a)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
