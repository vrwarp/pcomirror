# pcomirror — single-org Planning Center People mirror.
# Pure Python 3, standard library only: no build step, no pip dependencies.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PCOMIRROR_DB=/data/pcomirror.db \
    PCOMIRROR_HOST=0.0.0.0 \
    PCOMIRROR_PORT=8080

# Non-root runtime user; /data is where the SQLite file (a volume) lives.
RUN useradd --system --create-home --uid 10001 appuser \
 && mkdir -p /data && chown appuser:appuser /data

WORKDIR /app
COPY pcomirror ./pcomirror

USER appuser
VOLUME ["/data"]
EXPOSE 8080

# Liveness: hit our own /healthz (honours PCOMIRROR_PORT).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request,sys; p=os.environ.get('PCOMIRROR_PORT','8080'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz'%p,timeout=3).status==200 else 1)"]

# `serve` runs the JSON:API server + the background scheduler (sweeps, merger
# poll, drift, webhook + hydration drains). Override CMD for one-shot commands,
# e.g.  docker run ... pcomirror backfill
ENTRYPOINT ["python", "-m", "pcomirror"]
CMD ["serve"]
