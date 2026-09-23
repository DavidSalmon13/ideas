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
- **2026-09-23 — "Offline" CI is enforced in the tests.** A `tests/conftest.py` fixture blocks every non-loopback network connection, so a test that forgets a fake or cassette fails instead of quietly calling a real API. Installing dependencies in CI still uses the network.
- **2026-09-23 — CI also smoke-tests Docker Compose.** A second job runs `docker compose up --build` and waits for `/health`, covering the Phase 0 acceptance check that can't run in the dev container.
- **2026-09-23 — `unknown` is a fourth claim type.** specs.md lists `fact | estimate | assumption` for claims but also requires economics values tagged `verified | estimated | assumed | unknown`, and intent.md treats "could not verify" as a first-class answer. `Claim.type` therefore allows `unknown`, and `Claim.value_status` maps the four types to the four tags.
- **2026-09-23 — `None` vs. `unknown` in an Opportunity.** A claim-typed field is `None` until the stage that fills it runs; a stage that looked and found nothing records an `unknown` claim. This keeps "not researched yet" separate from "could not determine".
- **2026-09-23 — Unsourced facts are allowed by the model.** The auto-demote rule belongs to the Evidence validator (next Phase 1 step), so `Claim` accepts a `fact` with no sources rather than rejecting it.
- **2026-09-23 — Claims point to sources by id.** `Claim.source_ids` holds Source ids (specs.md §8.1 "Source refs"); Sources are stored once and shared.
- **2026-09-23 — Ranges on claims.** `Claim` has optional `value_low`/`value_high` next to `value`, because specs.md §14 asks for "ranges over points when uncertain".
- **2026-09-23 — Rules enforced in the models.** Blank `confidence_reason` is rejected; dimension scores are 1-5 and unique; any override (hard gate or fatal flaw) requires a `kill` verdict; a stage allows at most one kickback; a report halted at a stage must be marked incomplete. `HALTED_*` runs are not terminal because they can be resumed.
- **2026-09-23 — Not modelled yet.** `Profile` (gates/weights) is config and comes with the profiles work; `stage_state` lives in the Stage/AgentRun records rather than on the Opportunity. The default profile id is `bootstrap`.
