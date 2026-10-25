import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY is required in production")
    SECRET_KEY = "development-only-not-for-production"
ALLOWED_HOSTS = [s.strip() for s in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if s.strip()]
CSRF_TRUSTED_ORIGINS = [s.strip() for s in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if s.strip()]
INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "core",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.EntraSessionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"], "APP_DIRS": True,
    "OPTIONS": {"context_processors": ["django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth", "django.contrib.messages.context_processors.messages"]}}]
WSGI_APPLICATION = "config.wsgi.application"
if os.getenv("DJANGO_USE_SQLITE", "0") == "1":
    if not DEBUG:
        raise ImproperlyConfigured("SQLite is enabled only for local development")
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "dev.sqlite3"}}
else:
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql", "NAME": os.getenv("POSTGRES_DB", "oms"),
        "USER": os.getenv("POSTGRES_USER", "oms"), "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
        "HOST": os.getenv("POSTGRES_HOST", "db"), "PORT": os.getenv("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }}
AUTH_USER_MODEL = "core.User"
AUTHENTICATION_BACKENDS = ["core.authentication.EntraBackend"]
# Auth flow secrets must remain in server-side sessions, never signed-cookie sessions.
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 8 * 60 * 60
SESSION_SAVE_EVERY_REQUEST = False
ENTRA_SESSION_SECONDS = SESSION_COOKIE_AGE
ENTRA_TENANT_ID = os.getenv("ENTRA_TENANT_ID", "").strip()
ENTRA_CLIENT_ID = os.getenv("ENTRA_CLIENT_ID", "").strip()
ENTRA_CLIENT_SECRET = os.getenv("ENTRA_CLIENT_SECRET", "").strip()
ENTRA_REDIRECT_URI = os.getenv("ENTRA_REDIRECT_URI", "").strip()
ENTRA_ACCESS_ROLE = "OMS.Access"
ENTRA_LOGOUT_MICROSOFT = os.getenv("ENTRA_LOGOUT_MICROSOFT", "0") == "1"
ENTRA_POST_LOGOUT_REDIRECT_URI = os.getenv("ENTRA_POST_LOGOUT_REDIRECT_URI", "").strip()
ENTRA_CONFIGURED = all((ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET, ENTRA_REDIRECT_URI))
if any((ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET, ENTRA_REDIRECT_URI)):
    if not ENTRA_CONFIGURED:
        raise ImproperlyConfigured("Set all ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET, ENTRA_REDIRECT_URI values")
    from uuid import UUID
    from urllib.parse import urlsplit
    try:
        ENTRA_TENANT_ID = str(UUID(ENTRA_TENANT_ID))
        ENTRA_CLIENT_ID = str(UUID(ENTRA_CLIENT_ID))
    except ValueError:
        raise ImproperlyConfigured("ENTRA_TENANT_ID and ENTRA_CLIENT_ID must be UUIDs") from None

    def validate_entra_uri(value, path):
        uri = urlsplit(value)
        local_http = DEBUG and uri.scheme == "http" and uri.hostname in {"localhost", "127.0.0.1"}
        if (not (uri.scheme == "https" or local_http) or not uri.hostname
                or uri.path != path or uri.query or uri.fragment or uri.username or uri.password
                or uri.hostname not in ALLOWED_HOSTS):
            raise ImproperlyConfigured(f"Entra URI must use an allowed host, HTTPS (except debug localhost), and path {path}")

    validate_entra_uri(ENTRA_REDIRECT_URI, "/accounts/entra/callback/")
    if ENTRA_LOGOUT_MICROSOFT:
        validate_entra_uri(ENTRA_POST_LOGOUT_REDIRECT_URI, "/accounts/login/")
ENTRA_AUTHORITY = f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "ko-kr"
TIME_ZONE = "Asia/Seoul"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage" if DEBUG
        else "whitenoise.storage.CompressedManifestStaticFilesStorage"}}
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = os.getenv("DJANGO_SECURE_SSL_REDIRECT", "0" if DEBUG else "1") == "1"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "no-referrer"
X_FRAME_OPTIONS = "DENY"
if os.getenv("DJANGO_TRUST_PROXY", "0") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
LOGGING = {"version": 1, "disable_existing_loggers": False,
    "filters": {"auth_query": {"()": "core.logging.AuthQueryFilter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "filters": ["auth_query"]},
        "django.server": {"class": "logging.StreamHandler", "filters": ["auth_query"]}},
    "loggers": {"django.server": {"handlers": ["django.server"], "level": "INFO", "propagate": False},
        "authlib": {"level": "WARNING"}},
    "root": {"handlers": ["console"], "level": "INFO"}}
