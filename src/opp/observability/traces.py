"""Persisting traces (plan.md §19): one row per external call, written as it happens.

Each trace commits in its own short session, so spend is on record even if the agent's
current sub-task never commits (a crash mid-sub-task still leaves its cost accounted).
"""

from sqlalchemy.orm import Session, sessionmaker

from opp.db.repositories import TraceRepository
from opp.domain.report import Trace


class DbTraceSink:
    """A ``TraceSink`` that writes each trace straight to the ``traces`` table."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def __call__(self, trace: Trace) -> None:
        with self._sessions() as session:
            TraceRepository(session).add(trace)
            session.commit()
