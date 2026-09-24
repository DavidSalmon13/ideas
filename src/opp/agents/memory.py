"""An agent's working memory and its compaction (plan.md §7.2, intent.md §21.2).

The LLM context is disposable: agents build every prompt from ``AgentMemory.render()``
(a running summary plus recent sub-task notes), never from accumulated chat history.
The durable truth is in the DB: committed claims, the work ledger and the summary.

When memory grows past a threshold the runtime compacts it, folding the notes into a
new bounded summary that is persisted, so the next prompt starts from that compact state.
The default compaction is deterministic and free (a memory operation, not a model call);
an agent can override ``Agent.compact`` to summarise smarter.
"""

from dataclasses import dataclass, field

from opp.llm.pricing import CHARS_PER_TOKEN

SUMMARY_MAX_CHARS = 6_000
_ELIDED = "[…earlier work condensed…]\n"


@dataclass
class AgentMemory:
    summary: str = ""
    notes: list[str] = field(default_factory=list)

    def remember(self, note: str) -> None:
        if note.strip():
            self.notes.append(note.strip())

    def render(self) -> str:
        parts = []
        if self.summary:
            parts.append(f"Summary of work so far:\n{self.summary}")
        if self.notes:
            parts.append("Recent notes:\n" + "\n".join(f"- {note}" for note in self.notes))
        return "\n\n".join(parts)

    def estimated_tokens(self) -> int:
        return -(-len(self.render()) // CHARS_PER_TOKEN)

    def durable(self) -> str:
        """What's persisted as the agent run's summary: everything, flattened."""
        return fold(self.summary, self.notes)


def fold(summary: str, notes: list[str], *, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    """Merge notes into the summary and keep it bounded, keeping the most recent work
    (older detail already lives in committed claims)."""
    text = "\n".join(part for part in [summary, *(f"- {n}" for n in notes)] if part)
    if len(text) <= max_chars:
        return text
    keep = text[-(max_chars - len(_ELIDED)) :]
    # Start at a line boundary so the summary never opens mid-sentence.
    newline = keep.find("\n")
    if 0 <= newline < len(keep) - 1:
        keep = keep[newline + 1 :]
    return _ELIDED + keep
