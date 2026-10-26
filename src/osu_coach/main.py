from fastapi import FastAPI

from osu_coach import __version__
from osu_coach.api import health, players


def create_app() -> FastAPI:
    app = FastAPI(title="osu! AI Coach", version=__version__)
    app.include_router(health.router)
    app.include_router(players.router)
    return app


app = create_app()
