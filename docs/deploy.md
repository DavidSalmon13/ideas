# Deployment

A personal, single-operator tool, so deployment is deliberately minimal (plan.md §29):
**one container** runs the API + the in-process worker and **serves the built React SPA**,
with **SQLite** on a mounted volume as the only datastore. No Kubernetes, no broker, no
separate frontend host.

## What's in the image

The [`Dockerfile`](../Dockerfile) is two stages:

1. **`web`** (Node 22) runs `npm ci && npm run build` to produce `web/dist`.
2. The Python image installs the API with `uv` (frozen lockfile, no dev deps), copies in
   `web/dist`, and sets `OPP_SPA_DIST_DIR=/app/web/dist` so the API serves it.

On start the container runs `alembic upgrade head` (creating/upgrading the SQLite schema on
the volume) and then `uvicorn opp.api.app:app` on port 8000.

## Run it

```bash
docker compose up --build
```

[`docker-compose.yml`](../docker-compose.yml) binds **`127.0.0.1:8000`** only (no auth in the
MVP — localhost by design), mounts **`./data`** for the SQLite file, and reads secrets from an
optional `.env`. Then open:

- **UI:** <http://127.0.0.1:8000/app/> (the root `/` redirects here)
- **API:** <http://127.0.0.1:8000/runs>, `/opportunities`, `/profiles`, `/config`
- **Health:** <http://127.0.0.1:8000/health>

The SPA is served under **`/app`** on purpose: its client-side routes (`/runs/:id`,
`/opportunities/:id`, …) share paths with the API's own routes, so serving it at the root
would shadow them. Under `/app` there's no collision, and any `/app/...` path that isn't a
built file falls back to `index.html` for client-side routing.

## Configuration (environment)

Copy [`.env.example`](../.env.example) to `.env`. Everything a run depends on is config
(plan.md §30); the keys that matter for a deploy:

| Variable | Purpose |
|---|---|
| `OPP_ANTHROPIC_API_KEY` | LLM provider key (or the SDK's own `ANTHROPIC_API_KEY` / `ant auth`) |
| `OPP_TAVILY_API_KEY` | Search provider key |
| `OPP_DATABASE_URL` | Defaults to `sqlite:///./data/opp.db` (the mounted volume) |
| `OPP_SPA_DIST_DIR` | Set by the image to the built SPA; leave unset to serve no UI |
| `OPP_LOG_LEVEL`, `OPP_LOG_JSON` | Logging (JSON to stdout by default) |

Without a search key the engine-backed routes return `503`, but `/health`, `/profiles` and
the UI still load — so a misconfigured deploy fails loud, not silently.

## Operations

- **Logs:** structured JSON to stdout (`docker compose logs -f api`).
- **Backups:** copy the SQLite file — `cp data/opp.db data/opp.backup.db` (or snapshot the
  `data/` volume). That's the entire durable state, including in-progress runs.
- **Restart safety:** a run is durable; on startup the API runs a recovery scan and continues
  any run a previous process left mid-stage (plan.md §10). Interrupt and restart the
  container and runs pick up from their last committed sub-task.
- **Upgrades:** rebuild the image; `alembic upgrade head` runs on start, so schema migrations
  apply automatically against the mounted database.

## Remote access (V1, not MVP)

The MVP binds localhost with no auth. Exposing it remotely needs a **bearer token** and
**TLS termination** in front of the container (plan.md §18, §29); both are deferred to V1.
