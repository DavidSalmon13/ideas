# Decisions

Choices made where the docs didn't settle the question. Newest last.

- **2026-09-23 — Minimal API in the foundation step.** Phase 0's acceptance ("`docker compose up` starts API") needs something to run, so the repo step ships a FastAPI app with only `GET /health` (`opp.api.app`). Real routes come in their phases.
- **2026-09-23 — Compose binds to localhost.** `127.0.0.1:8000` only, per plan.md §18 (no auth in MVP, localhost bind by default).
- **2026-09-23 — Ruff skips Markdown.** Ruff would reformat the Python snippets inside `plan.md`; the design docs are excluded from linting/formatting.
- **2026-09-23 — Python pinned to 3.12.x** (`>=3.12,<3.13`) to match plan.md §2 and the Docker base image.
- **2026-09-23 — Alembic baseline is empty.** Revision `0001` creates no tables; plan.md §24 puts the domain tables in Phase 1, which will add them as `0002`. The baseline just proves `alembic upgrade head` runs.
- **2026-09-23 — Settings use the `OPP_` prefix.** e.g. `OPP_DATABASE_URL`, `OPP_LOG_LEVEL`. Only settings used today exist; provider keys, budgets and model routing are added in the steps that need them.
- **2026-09-23 — Default database is `./data/opp.db`.** The `data/` folder is git-ignored and mounted as the Docker volume, so backups are "copy that file" (plan.md §29). The container runs `alembic upgrade head` on start.
- **2026-09-23 — Logs redact secret-looking fields.** Any log field whose name contains key/secret/token/password is written as `***` (plan.md §18, §31).
