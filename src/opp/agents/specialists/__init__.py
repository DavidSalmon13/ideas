"""Deep-research specialists: customer, competition, market, feasibility (specs.md §5.4,
intent.md §9.4).

Four specialists research the one selected opportunity in parallel, each going deep on a
single axis so a strong axis can never paper over a weak one. Each is a ``SpecialistAgent``
parameterised by its ``Axis`` (its focus and its research questions). They emit structured
claims filed *loose* under the opportunity — not into the opportunity document — so the
parallel runs never overwrite one another; Synthesis reconciles them afterwards.
"""

from opp.agents.specialists.axes import AXES, Axis
from opp.agents.specialists.specialist import SpecialistAgent

__all__ = ["AXES", "Axis", "SpecialistAgent"]
