# Single container: the API + in-process worker, serving the built SPA (plan.md §29).

# Stage 1 — build the React SPA (web/dist).
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# Stage 2 — the Python API image.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
RUN uv sync --frozen --no-dev

# The built SPA, served by the API under /app.
COPY --from=web /web/dist ./web/dist
ENV OPP_SPA_DIST_DIR=/app/web/dist

EXPOSE 8000
CMD ["sh", "-c", "uv run --no-sync alembic upgrade head && exec uv run --no-sync uvicorn opp.api.app:app --host 0.0.0.0 --port 8000"]
