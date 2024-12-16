# syntax=docker/dockerfile:1.4
FROM --platform=$BUILDPLATFORM python:3-alpine AS builder


RUN apk update
RUN apk add pkgconfig
RUN apk add --virtual build-deps gcc python3-dev musl-dev tesseract-ocr tesseract-ocr-data-eng

WORKDIR /app
COPY . /app

RUN pip3 install -r requirements.txt
RUN pip3 install gunicorn

RUN chmod u+x ./entrypoint.sh
RUN chmod u+x ./entrypoint_celery.sh

RUN adduser -D python
RUN chown -R python:python /app

USER python
ENTRYPOINT ["./entrypoint.sh"]
