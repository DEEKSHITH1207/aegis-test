"""Celery application.

Broker and serialisation options come from Django settings via the CELERY_
namespace, so there is a single place to configure the queue.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("aegis")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
