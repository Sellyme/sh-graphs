FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    SH_HOST=0.0.0.0 \
    SH_PORT=8765 \
    SH_NO_BROWSER=1 \
    SH_DATA_FILE=/data/sh-graphs-data.json

# Run as an unprivileged user. /data is created first so a fresh volume inherits its ownership.
RUN useradd --system --uid 10001 --no-create-home app \
 && mkdir /app /data \
 && chown app:app /data

WORKDIR /app
COPY index.html favicon.ico server.py ./

USER app
VOLUME /data
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8765/healthz', timeout=3)"

CMD ["python", "server.py"]
