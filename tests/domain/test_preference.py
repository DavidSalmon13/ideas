"""The learned operator-preference profile (intent.md §19.2): signals carry the right
sentiment for their origin, and the profile renders as a compact, deduped steering block."""

from opp.domain.enums import PreferenceOrigin, PreferenceSentiment
from opp.domain.preference import PreferenceSignal, render_preferences


def test_signal_sentiment_follows_origin() -> None:
    veto = PreferenceSignal.of("too much manual sales", PreferenceOrigin.CHECKPOINT_VETO)
    pick = PreferenceSignal.of("loved the automation angle", PreferenceOrigin.CHECKPOINT_PICK)
    discard = PreferenceSignal.of("market too small even for me", PreferenceOrigin.DISCARD)

    assert veto.sentiment is PreferenceSentiment.DISLIKE
    assert pick.sentiment is PreferenceSentiment.NEUTRAL
    assert discard.sentiment is PreferenceSentiment.DISLIKE
    # The reason is whitespace-normalised.
    assert PreferenceSignal.of("  a\n b ", PreferenceOrigin.DISCARD).reason == "a b"


def test_render_groups_by_sentiment_and_dedups() -> None:
    signals = [
        PreferenceSignal.of("too much manual sales", PreferenceOrigin.CHECKPOINT_VETO),
        PreferenceSignal.of("too much manual sales", PreferenceOrigin.DISCARD),  # duplicate reason
        PreferenceSignal.of("loved the automation angle", PreferenceOrigin.CHECKPOINT_PICK),
    ]
    rendered = render_preferences(signals)
    assert "Avoid opportunities like these: too much manual sales" in rendered
    assert "Lean toward / context: loved the automation angle" in rendered
    # Deduped: the disliked reason appears once.
    assert rendered.count("too much manual sales") == 1


def test_render_empty_is_blank() -> None:
    assert render_preferences([]) == ""
