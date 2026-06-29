# syntax=docker/dockerfile:1.4
FROM python:3.13-alpine AS builder

RUN apk add --no-cache \
    gcc \
    bash \
    musl-dev \
    libpq-dev \
    pkgconfig \
    python3-dev

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=source=requirements.txt,target=/tmp/requirements.txt \
    pip install -r /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install gunicorn

FROM python:3.13-alpine AS runtime

RUN adduser -D python

RUN apk add --no-cache \
    bash \
    tini \
    curl \
    postgresql-client \
    tesseract-ocr \
    tesseract-ocr-data-eng \
    supercronic && \
    rm -rf /tmp/* /var/cache/apk/*

COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH"

COPY --chown=python:python . /app

WORKDIR /app

RUN pybabel compile -d app/translations || true

RUN chmod u+x ./entrypoint.sh
RUN chmod u+x ./entrypoint_celery.sh
RUN chmod u+x ./entrypoint_async.sh

USER python
EXPOSE 5000
ENV CRON="false"

LABEL org.opencontainers.image.authors="rimu"
LABEL org.opencontainers.image.source="https://codeberg.org/rimu/pyfedi"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
LABEL org.opencontainers.image.description="A Lemmy/Mbin alternative written in Python with Flask."

HEALTHCHECK --interval=60s --retries=2 --timeout=10s CMD curl -ILfSs http://localhost:5000/health >/dev/null || exit 1

ENTRYPOINT ["/sbin/tini", "--", "/app/entrypoint.sh"]