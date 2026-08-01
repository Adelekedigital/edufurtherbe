"""Application composition root.

Exempt from the layer check: this is the one place allowed to see everything.
"""

from fastapi import FastAPI

from app.api.routes import health
from app.core.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Takes settings as an argument so tests construct an app without touching the
    process environment or the cached ``get_settings`` singleton.
    """
    resolved = settings or get_settings()
    application = FastAPI(
        title="EduFurther API",
        version="0.1.0",
        debug=resolved.debug,
    )
    application.include_router(health.router)
    return application


app = create_app()
