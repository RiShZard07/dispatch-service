# dispatch-service: synthetic local-delivery dispatch backend (educational prototype).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY schema.sql ./
COPY app ./app
COPY scripts ./scripts

# Run as an unprivileged user; the SQLite file lives in a directory that user owns.
RUN useradd --create-home --uid 10001 dispatch \
    && mkdir -p /srv/data \
    && chown -R dispatch:dispatch /srv/data
USER dispatch

# Hosting platforms such as Render tell the container which port to listen on
# through PORT; 8000 is the default for docker run and docker compose.
ENV DISPATCH_DATABASE_URL=sqlite:////srv/data/dispatch.sqlite PORT=8000
EXPOSE 8000
# exec replaces the shell, so uvicorn is PID 1 and receives the platform's stop signal.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log"]
