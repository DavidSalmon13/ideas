"""Cassettes: recorded external calls that tests and offline runs replay (plan.md §20).

A cassette is one JSON file of *interactions*. Each interaction has a ``kind`` (``llm``,
``search`` or ``fetch``), a deterministic ``key`` derived from the request, a short
human-readable ``request`` summary, and either the ``response`` or the ``error`` that
came back. Recording happens at the provider interfaces, not at the HTTP layer, so a
cassette never holds API keys, headers or auth tokens, and replays regardless of which
adapter produced it.

Modes:

- ``record`` — call the real provider and append every interaction;
- ``replay`` — answer only from the cassette; a request with no recording raises
  ``CassetteMissError`` (never a silent live call);
- ``once`` — replay what's recorded, record what's missing (for building a cassette).

When the same request was recorded several times, replay returns the recordings in
order and then keeps returning the last one.
"""

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1


class CassetteMode(StrEnum):
    RECORD = "record"
    REPLAY = "replay"
    ONCE = "once"


class CassetteMissError(LookupError):
    """Replay found no recording for a request. Re-record the cassette."""


@dataclass
class Interaction:
    kind: str
    key: str
    request: dict[str, Any]
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "key": self.key, "request": self.request}
        if self.error is not None:
            data["error"] = self.error
        else:
            data["response"] = self.response
        return data


@dataclass
class Cassette:
    path: Path
    mode: CassetteMode = CassetteMode.REPLAY
    interactions: list[Interaction] = field(default_factory=list)
    _cursor: dict[tuple[str, str], int] = field(default_factory=dict, repr=False)
    _dirty: bool = field(default=False, repr=False)

    @classmethod
    def open(cls, path: str | Path, mode: CassetteMode = CassetteMode.REPLAY) -> "Cassette":
        path = Path(path)
        cassette = cls(path=path, mode=mode)
        if mode is CassetteMode.RECORD:
            return cassette  # a fresh recording replaces the old file on save
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") != FORMAT_VERSION:
                raise ValueError(f"{path}: unsupported cassette version {data.get('version')!r}")
            cassette.interactions = [Interaction(**item) for item in data["interactions"]]
        elif mode is CassetteMode.REPLAY:
            raise FileNotFoundError(f"cassette {path} does not exist; record it first")
        return cassette

    @property
    def can_record(self) -> bool:
        return self.mode in (CassetteMode.RECORD, CassetteMode.ONCE)

    def find(self, kind: str, key: str) -> Interaction | None:
        """The next recording for this request, or ``None`` if there is none."""
        matches = [i for i in self.interactions if i.kind == kind and i.key == key]
        if not matches or self.mode is CassetteMode.RECORD:
            return None
        index = self._cursor.get((kind, key), 0)
        self._cursor[(kind, key)] = index + 1
        return matches[min(index, len(matches) - 1)]

    def miss(self, kind: str, request: dict[str, Any]) -> CassetteMissError:
        summary = json.dumps(request, sort_keys=True)[:300]
        return CassetteMissError(f"no {kind} recording in {self.path} for {summary}")

    def add(self, interaction: Interaction) -> None:
        self.interactions.append(interaction)
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": FORMAT_VERSION,
            "interactions": [i.to_json() for i in self.interactions],
        }
        self.path.write_text(
            json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self._dirty = False

    def __enter__(self) -> "Cassette":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.save()


def request_key(payload: dict[str, Any]) -> str:
    """A stable digest of a request: same payload, same key, on any machine."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
