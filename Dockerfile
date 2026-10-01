# syntax=docker/dockerfile:1
# The Zagrosian Eye — container image
# Pinned to 3.12 rather than 3.13+: audioop is still in the stdlib on 3.12, so
# discord.py imports without the audioop-lts shim, and every pinned dependency
# (asyncpg, cryptography, SQLAlchemy) ships prebuilt manylinux wheels for 3.12.
# On 3.13+ requirements.txt installs audioop-lts to cover the removed module.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# curl is only for the operator's convenience when debugging a container by hand.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The process writes to ./data (SQLite + backups) and ./logs. Create them owned
# by the unprivileged user so a mounted volume does not hide the permissions.
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/data /app/data/backups /app/logs \
    && chown -R bot:bot /app
USER bot

# No EXPOSE and no HEALTHCHECK: the bot's gateway socket is outbound. The
# optional health server (core/health_server.py) binds $PORT when the host sets
# it, which is how a Render free web service is kept alive by an external
# monitor — see DEPLOY.md §3.3. Nothing here needs a port for liveness, so
# container/orchestrator health checks have nothing to probe; process restarts
# are the supervisor's job.

CMD ["python", "main.py"]