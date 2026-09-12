"""Importing the Celery app here ensures it is configured whenever Django starts."""

from config.celery import app as celery_app

__all__ = ("celery_app",)
