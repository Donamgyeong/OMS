#!/bin/sh
set -eu
python manage.py check --deploy --fail-level ERROR
exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 \
  --workers "${GUNICORN_WORKERS:-1}" --threads "${GUNICORN_THREADS:-2}" \
  --timeout 60 --access-logfile - --error-logfile - \
  --access-logformat '%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s'
