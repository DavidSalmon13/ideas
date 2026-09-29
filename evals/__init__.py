"""AI-quality evaluation suite (plan.md §21): golden scenarios + the metrics that judge
whether the *research is good*, distinct from the correctness tests in ``tests/``.

Runs fully offline on the funnel fakes (no network, no keys), so it can gate CI and be
re-run whenever prompts/models/routing change to catch quality regressions."""
