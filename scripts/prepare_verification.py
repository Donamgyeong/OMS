"""Generate isolated local verification credentials without printing secrets."""
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parent.parent
target = ROOT / ".env.verify"
if target.exists():
    print("Existing verification configuration retained.")
else:
    values = {
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
        "DJANGO_DEBUG": "0",
        "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
        "DJANGO_CSRF_TRUSTED_ORIGINS": "https://localhost",
        "DJANGO_SECURE_SSL_REDIRECT": "1",
        "DJANGO_TRUST_PROXY": "1",
        "POSTGRES_DB": "oms_verify",
        "POSTGRES_USER": "oms_verify",
        "POSTGRES_PASSWORD": secrets.token_urlsafe(40),
        "APP_IMAGE": "oms:verify",
        "APP_PORT": "18000",
        "GUNICORN_WORKERS": "1",
        "GUNICORN_THREADS": "2",
    }
    target.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")
    print("Created isolated .env.verify (gitignored).")
