# syntax=docker/dockerfile:1.4
FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    libpq-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=source=requirements.txt,target=/tmp/requirements.txt \
    pip install -r /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install gunicorn

FROM python:3.13-slim AS runtime

ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.49

RUN adduser --disabled-password --gecos "" python

RUN apt-get update && apt-get install -y --no-install-recommends \
    tini \
    curl \
    ca-certificates \
    postgresql-client \
    tesseract-ocr \
    tesseract-ocr-eng && \
    curl -fsSLO "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}" && \
    case "${TARGETARCH}" in \
      amd64) echo "e63c11a9726b775a6a11801e81af4f3fb926aa68  supercronic-linux-${TARGETARCH}" ;; \
      arm64) echo "0b6c5bb743e0b0dafed1132198c81807927ac413  supercronic-linux-${TARGETARCH}" ;; \
      *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac | sha1sum -c - && \
    chmod +x "supercronic-linux-${TARGETARCH}" && \
    mv "supercronic-linux-${TARGETARCH}" /usr/local/bin/supercronic && \
    rm -rf /var/lib/apt/lists/* /tmp/*

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

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
