# Decisions

Choices made where the docs didn't settle the question. Newest last.

- **2026-09-23 — Minimal API in the foundation step.** Phase 0's acceptance ("`docker compose up` starts API") needs something to run, so the repo step ships a FastAPI app with only `GET /health` (`opp.api.app`). Real routes come in their phases.
- **2026-09-23 — Compose binds to localhost.** `127.0.0.1:8000` only, per plan.md §18 (no auth in MVP, localhost bind by default).
- **2026-09-23 — Ruff skips Markdown.** Ruff would reformat the Python snippets inside `plan.md`; the design docs are excluded from linting/formatting.
- **2026-09-23 — Python pinned to 3.12.x** (`>=3.12,<3.13`) to match plan.md §2 and the Docker base image.
