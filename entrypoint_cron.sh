#!/usr/bin/env sh
set -e

echo "Starting cron jobs..."

exec supercronic /app/docker.cron
