FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock
RUN groupadd --gid 10001 oms && useradd --uid 10001 --gid oms --create-home oms
COPY --chown=oms:oms . .
RUN mkdir -p /app/staticfiles /app/media && chown -R oms:oms /app/staticfiles /app/media
USER oms
RUN DJANGO_SECRET_KEY=build-time-placeholder-not-a-runtime-secret python manage.py collectstatic --noinput
EXPOSE 8000
CMD ["sh", "scripts/start.sh"]
