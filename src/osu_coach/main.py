from fastapi import FastAPI

from osu_coach import __version__
from osu_coach.api import coach, health, players, recommendations, replays, skills


def create_app() -> FastAPI:
    app = FastAPI(title="osu! AI Coach", version=__version__)
    app.include_router(health.router)
    app.include_router(players.router)
    app.include_router(replays.router)
    app.include_router(skills.router)
    app.include_router(recommendations.router)
    app.include_router(coach.router)
    return app


app = create_app()
