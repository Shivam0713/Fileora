from celery import Celery
from celery.schedules import crontab
from app.core.config import settings

celery = Celery("fileora", broker=settings.redis_url, backend=settings.redis_url, include=["app.workers.tasks"])
celery.conf.update(task_serializer="json", result_serializer="json", accept_content=["json"], task_track_started=True, task_time_limit=180, worker_prefetch_multiplier=1, timezone="UTC", enable_utc=True, beat_schedule={"remove-expired-files":{"task":"fileora.cleanup_expired","schedule":crontab(minute="*/15")}})
