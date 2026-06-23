# syntax=docker/dockerfile:1.4
FROM --platform=$BUILDPLATFORM python:3.13-alpine AS builder


RUN adduser -D python

RUN apk add --no-cache pkgconfig gcc python3-dev musl-dev tesseract-ocr tesseract-ocr-data-eng postgresql-client libpq-dev bash

RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=source=requirements.txt,target=/tmp/requirements.txt \
    pip3 install -r /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip3 install gunicorn

COPY --chown=python:python . /app

WORKDIR /app

RUN pybabel compile -d app/translations || true

RUN chmod u+x ./entrypoint.sh
RUN chmod u+x ./entrypoint_celery.sh
RUN chmod u+x ./entrypoint_async.sh

USER python
ENTRYPOINT ["./entrypoint.sh"]
