"""The four deep-research axes (intent.md §9.4). Each is data: a key, a one-line focus for
the prompt, and the research questions that become one sub-task each."""

from dataclasses import dataclass

SPECIALIST = "specialist"


@dataclass(frozen=True)
class Axis:
    key: str
    focus: str
    questions: tuple[str, ...]

    @property
    def agent_name(self) -> str:
        """How this specialist's agent run and claims are labelled, e.g. ``specialist:customer``."""
        return f"{SPECIALIST}:{self.key}"


AXES: tuple[Axis, ...] = (
    Axis(
        key="customer",
        focus=(
            "the exact buyer and user, how badly the pain bites, what the segment does about "
            "it today, and whether it can and will pay"
        ),
        questions=(
            "who exactly has this problem and how they describe it in their own words",
            "how this segment handles the problem today and what it already spends on it",
            "evidence that this segment pays for tools or services like this",
        ),
    ),
    Axis(
        key="competition",
        focus=(
            "the existing solutions, their pricing and limits, why a customer would switch, "
            "and why incumbents have not already solved it"
        ),
        questions=(
            "existing tools, competitors and substitutes serving this segment, and their pricing",
            "documented complaints about and limitations of the current solutions",
            "why incumbents have not solved this problem well for this segment",
        ),
    ),
    Axis(
        key="market",
        focus="the market's structure and size and the real price and cost anchors for economics",
        questions=(
            "how large and how structured this market is",
            "real prices and real costs that anchor the unit economics",
            "how businesses in this space reach and sell to the segment",
        ),
    ),
    Axis(
        key="feasibility",
        focus=(
            "the specific recent change that opened this now, the technical feasibility and "
            "barriers, and what a one-to-three person team can realistically build and run"
        ),
        questions=(
            "the specific recent change (regulation, technology, price or behaviour) that makes "
            "now the moment",
            "what building this requires technically and the barriers to entry",
            "what a one-to-three person team can realistically build and operate here",
        ),
    ),
)
