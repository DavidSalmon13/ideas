"""Deterministic run state machine, executor, kickbacks, resume.

- ``opp.orchestration.machine`` — the pipeline and its allowed transitions (pure).
- ``opp.orchestration.stages`` — the stage-handler interface and the built stages.
- ``opp.orchestration.engine`` — ``Orchestrator``: execute, resume, cancel, recover.
"""
