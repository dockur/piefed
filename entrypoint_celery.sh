#!/bin/bash

exec celery -A celery_worker_docker.celery worker --concurrency=4 --queues=celery,background,send
