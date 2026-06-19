FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=production \
    FLASK_APP=app.app \
    FLASK_RUN_HOST=0.0.0.0 \
    FLASK_RUN_PORT=5000 \
    DB_PATH=/data/people.sqlite3 \
    LESSONS_LEARNED_DB_PATH=/data/lessons_learned.sqlite3 \
    AUTH_SESSION_DB_PATH=/data/auth_sessions.sqlite3 \
    JAZZ_BASE_URL=https://jazz.example.internal/ \
    JTS_APP_URL=https://jazz.example.internal/jts/ \
    RS_BASE_URL=https://jazz.example.internal \
    OPENAI_MODEL=gpt-5-mini \
    OPENAI_MAX_OUTPUT_TOKENS=35000 \
    VERIFY_SSL=true \
    SESSION_COOKIE_SAMESITE=Lax \
    SESSION_COOKIE_SECURE=true

WORKDIR /srv/project

RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends ca-certificates fonts-dejavu-core && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --system appuser && \
    useradd --system --gid appuser --create-home --home-dir /home/appuser --shell /usr/sbin/nologin appuser && \
    mkdir -p /data /run/secrets && \
    chown -R appuser:appuser /srv/project /data /run/secrets

COPY app/requirements.txt ./app/requirements.txt
RUN python -m pip install --upgrade pip && \
    python -m pip install -r app/requirements.txt

COPY --chown=appuser:appuser app ./app

USER appuser

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 5000), timeout=5).close()"]

CMD ["python", "-m", "flask", "run"]
