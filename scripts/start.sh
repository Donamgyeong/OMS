#!/bin/sh
set -eu
exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 \
  --workers "${GUNICORN_WORKERS:-1}" --threads "${GUNICORN_THREADS:-2}" \
  --timeout 60 --access-logfile - --error-logfile -
