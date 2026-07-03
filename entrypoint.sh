#!/usr/bin/env bash
set -e

export FLASK_APP=pyfedi.py
echo "Running database migrations..."

flask db upgrade
flask populate_community_search

# Schedule cron jobs in background
if [[ "${CRON:-}" == [YyTt1]* ]]; then
  supercronic -quiet -no-reap /app/docker.cron &
fi

if [ "${FLASK_DEBUG:-}" = "1" ] && [ "${FLASK_ENV:-}" = "development" ]; then
  export FLASK_RUN_EXTRA_FILES=$(find app/templates app/static -type f | tr '\n' ':')
  echo "Starting flask development server..."
  exec flask run -h 0.0.0.0 -p 5000
else
  echo "Starting Gunicorn..."
  exec gunicorn --config gunicorn.conf.py --preload pyfedi:app
fi