FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Requirements are copied first so the dependency layer is cached and only
# rebuilt when the pinned versions actually change.
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt

COPY . .

# Run as a non-root user. UID 1000 matches the typical host user, so files
# written through the bind mount in docker-compose stay editable.
RUN useradd --create-home --uid 1000 aegis && chown -R aegis:aegis /app
USER aegis

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
