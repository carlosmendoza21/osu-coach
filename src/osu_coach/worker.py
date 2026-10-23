from celery import Celery

from osu_coach.config import get_settings

settings = get_settings()

celery_app = Celery("osu_coach", broker=settings.redis_url, backend=settings.redis_url)
