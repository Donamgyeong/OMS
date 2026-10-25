"""Generate isolated local verification credentials without printing secrets."""
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parent.parent
target = ROOT / ".env.verify"
# Synthetic credentials permit startup/config checks; they cannot authenticate
# against a real tenant. OIDC tests use a fake HTTPS transport with signed tokens.
entra_fixture = {
    "ENTRA_TENANT_ID": "11111111-1111-4111-8111-111111111111",
    "ENTRA_CLIENT_ID": "22222222-2222-4222-8222-222222222222",
    "ENTRA_CLIENT_SECRET": "verification-only-no-real-tenant",
    "ENTRA_REDIRECT_URI": "https://localhost/accounts/entra/callback/",
}
if target.exists():
    content = target.read_text(encoding="utf-8")
    keys = {line.split("=", 1)[0] for line in content.splitlines() if "=" in line}
    if not keys.intersection(entra_fixture):
        target.write_text(content.rstrip() + "\n" + "\n".join(f"{key}={value}" for key, value in entra_fixture.items()) + "\n", encoding="utf-8")
        print("Added synthetic Entra verification configuration.")
    print("Existing verification configuration retained.")
else:
    values = {
        **entra_fixture,
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
