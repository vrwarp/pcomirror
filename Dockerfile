# pcomirror — single-org Planning Center People mirror.
# Pure Python 3, standard library only: no build step, no pip dependencies.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PCOMIRROR_DB=/data/pcomirror.db \
    PCOMIRROR_HOST=0.0.0.0 \
    PCOMIRROR_PORT=8080

# Default runtime user; /data is where the SQLite file (a volume) lives. PUID/PGID
# override this uid/gid at start-up (see docker-entrypoint.py).
RUN useradd --system --create-home --uid 10001 appuser \
 && mkdir -p /data && chown appuser:appuser /data

WORKDIR /app
COPY pcomirror ./pcomirror
COPY docker-entrypoint.py /usr/local/bin/docker-entrypoint.py

# Starts as root *only* to chown the data directory to PUID:PGID; the entrypoint
# then permanently drops to that user before exec'ing the app, so the service
# itself never runs as root. Pass `--user` to skip that dance entirely.
VOLUME ["/data"]
EXPOSE 8080

# Liveness: hit our own /healthz (honours PCOMIRROR_PORT). Exits quietly on
# failure — an unhandled traceback here every 30s buries the real startup error.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request,sys\ntry:\n p=os.environ.get('PCOMIRROR_PORT','8080')\n sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz'%p,timeout=3).status==200 else 1)\nexcept Exception as e:\n print('healthcheck:',e)\n sys.exit(1)"]

# `serve` runs the JSON:API server + the background scheduler (sweeps, merger
# poll, drift, webhook + hydration drains). Override CMD for one-shot commands,
# e.g.  docker run ... pcomirror backfill
ENTRYPOINT ["python", "/usr/local/bin/docker-entrypoint.py"]
CMD ["serve"]
