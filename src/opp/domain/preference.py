"""The learned operator-preference profile (intent.md §19.2, specs.md §23.2).

Every checkpoint veto/re-pick and every discard/park carries the operator's one-line
reason. Those reasons accumulate, across runs, into a human-readable profile that Discovery
and Triage read as **soft steering** — never a hard filter. The profile is just the recent
signals rendered as text; there is no scoring model behind it (outcome-based learning is
deferred, intent.md §19.2).
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from uuid import UUID

from pydantic import Field

from opp.domain.base import Entity, utcnow
from opp.domain.enums import PreferenceOrigin, PreferenceSentiment

# Which sentiment each origin carries: a veto/discard is a "don't", a pick/park is context.
_SENTIMENT: dict[PreferenceOrigin, PreferenceSentiment] = {
    PreferenceOrigin.CHECKPOINT_VETO: PreferenceSentiment.DISLIKE,
    PreferenceOrigin.CHECKPOINT_PICK: PreferenceSentiment.NEUTRAL,
    PreferenceOrigin.DISCARD: PreferenceSentiment.DISLIKE,
    PreferenceOrigin.PARK: PreferenceSentiment.NEUTRAL,
}


class PreferenceSignal(Entity):
    """One learned steer: the operator's one-line reason, tagged with where it came from."""

    reason: str = Field(min_length=1)
    origin: PreferenceOrigin
    sentiment: PreferenceSentiment
    run_id: UUID | None = None
    opportunity_id: UUID | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @classmethod
    def of(
        cls,
        reason: str,
        origin: PreferenceOrigin,
        *,
        run_id: UUID | None = None,
        opportunity_id: UUID | None = None,
    ) -> "PreferenceSignal":
        return cls(
            reason=" ".join(reason.split()),
            origin=origin,
            sentiment=_SENTIMENT[origin],
            run_id=run_id,
            opportunity_id=opportunity_id,
        )


def render_preferences(signals: Sequence[PreferenceSignal]) -> str:
    """The profile as a short steering block, newest first, grouped by sentiment. Empty when
    there is nothing learned yet, so callers can fall back to "none"."""
    if not signals:
        return ""
    ordered = sorted(signals, key=lambda s: s.created_at, reverse=True)
    lines: list[str] = []
    for label, sentiment in (
        ("Avoid opportunities like these", PreferenceSentiment.DISLIKE),
        ("Lean toward / context", PreferenceSentiment.NEUTRAL),
        ("Favour opportunities like these", PreferenceSentiment.LIKE),
    ):
        picked = _dedup(s.reason for s in ordered if s.sentiment is sentiment)
        if picked:
            lines.append(f"{label}: " + "; ".join(picked))
    return "\n".join(lines)


def _dedup(reasons: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for reason in reasons:
        seen.setdefault(reason.strip(), None)
    return [reason for reason in seen if reason]
