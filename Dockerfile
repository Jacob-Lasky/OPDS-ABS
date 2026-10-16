# Python 3.15 (newest stable, Oct 2026). Every runtime wheel (aiohttp, lxml,
# pydantic-core) publishes cp315 manylinux builds, so nothing compiles here.
FROM python:3.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PUID=1000 \
    PGID=1000

# gosu drops root to PUID:PGID in the entrypoint.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

# Hash-pinned; see requirements.in for how to regenerate.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

WORKDIR /app
COPY ./opds_abs ./opds_abs
COPY ./run.py .
COPY ./docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /app/opds_abs/data && chmod 770 /app/opds_abs/data

ARG VERSION=dev
ENV OPDS_ABS_VERSION=${VERSION}

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "run.py"]
