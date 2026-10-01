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

# No EXPOSE and no HEALTHCHECK: this process holds an outbound gateway socket and
# opens no listening port, so there is nothing to probe. Liveness is the
# supervisor's job (systemd Restart=always, or the host's own restart policy).

CMD ["python", "main.py"]