import os

from celery import Celery
from celery.signals import task_prerun, task_postrun

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tcomp.settings.prod')

app = Celery('tcomp')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()


@task_prerun.connect
def close_old_connections_prerun(**kwargs):
    """Close stale DB connections before each task to prevent
    'connection already closed' errors in long-lived workers."""
    from django.db import close_old_connections
    close_old_connections()


@task_postrun.connect
def close_old_connections_postrun(**kwargs):
    """Close DB connections after each task to return them to the pool."""
    from django.db import close_old_connections
    close_old_connections()
