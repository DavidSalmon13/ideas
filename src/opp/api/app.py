"""FastAPI application factory."""

from fastapi import FastAPI

from opp.config.settings import get_settings
from opp.observability.logging import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    app = FastAPI(title="Opportunity Discovery & Evaluation System")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
