"""Semantic dedup keys for leads (specs.md §4.5, plan.md §14).

The key is built from the named customer, the pain and the business shape, normalised
so that wording differences (case, punctuation, word order, plurals, filler words)
don't hide a duplicate.
"""

import hashlib
import re

from opp.domain.lead import Lead

# fmt: off
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it",
    "of", "on", "or", "that", "the", "their", "them", "they", "this", "to", "who", "with",
})
# fmt: on
_WORD = re.compile(r"[a-z0-9]+")


def normalize_text(text: str) -> str:
    """Sorted, de-duplicated content words with a crude plural strip."""
    words = {_singular(word) for word in _WORD.findall(text.lower()) if word not in _STOPWORDS}
    return " ".join(sorted(words))


def lead_dedup_hash(lead: Lead) -> str:
    key = "|".join(
        (
            normalize_text(lead.named_customer.statement),
            normalize_text(lead.specific_pain.statement),
            lead.business_shape.value,
        )
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word
