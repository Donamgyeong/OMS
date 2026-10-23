"""Exercise the running production WSGI server inside the app container.

Run with: docker compose ... exec -T app python scripts/smoke_container.py
The forwarded HTTPS header simulates the trusted DSM reverse proxy; it does
not test real TLS or DSM. A random test account is deleted in finally.
"""
import os
import platform
import re
import secrets
import tempfile
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import django
django.setup()
from django.conf import settings
from django.contrib.auth import get_user_model


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = build_opener(NoRedirect())
cookies = SimpleCookie()


def request(path, data=None, forwarded=True):
    headers = {"Host": "localhost"}
    if forwarded:
        headers["X-Forwarded-Proto"] = "https"
    if cookies:
        headers["Cookie"] = "; ".join(f"{key}={value.value}" for key, value in cookies.items())
    if data is not None:
        headers.update({"Origin": "https://localhost", "Referer": "https://localhost/accounts/login/"})
    req = Request("http://127.0.0.1:8000" + path, headers=headers,
        data=urlencode(data).encode() if data is not None else None)
    try:
        response = opener.open(req, timeout=15)
    except HTTPError as error:
        response = error
    with response:
        for value in response.headers.get_all("Set-Cookie", []):
            cookies.load(value)
        return response.status, response.headers, response.read().decode()


def check(condition, label):
    if not condition:
        raise AssertionError(label)
    print("PASS:", label)


if __name__ == "__main__":
    check(platform.machine() == "aarch64", "ARM64 runtime")
    check(os.getuid() == 10001, "non-root application user")
    status, _, _ = request("/health/", forwarded=False)
    check(status == 301, "direct HTTP redirects to HTTPS")
    status, _, body = request("/health/")
    check(status == 200 and '"ok"' in body, "HTTP health endpoint and PostgreSQL connection")
    status, _, _ = request("/")
    check(status == 302, "anonymous dashboard requires login")
    status, _, body = request("/accounts/login/")
    check(status == 200, "production login page")
    assets = re.findall(r'(?:href|src)="(/static/[^\"]+)"', body)
    check(len(assets) >= 3, "hashed Bootstrap, HTMX and application assets rendered")
    for asset in assets:
        status, _, _ = request(asset)
        check(status == 200, "static asset " + asset)
    csrf = re.search(r'name="csrfmiddlewaretoken" value="([^\"]+)"', body).group(1)
    status, _, _ = request("/accounts/login/", {"username": "invalid", "password": "invalid"})
    check(status == 403, "CSRF protection rejects missing token")
    username = "verify_" + secrets.token_hex(6)
    password = secrets.token_urlsafe(32)
    user = get_user_model().objects.create_superuser(username=username, password=password)
    try:
        status, _, _ = request("/accounts/login/", {"username": username,
            "password": password, "csrfmiddlewaretoken": csrf})
        check(status == 302 and bool(cookies.get("sessionid")), "real HTTP session login")
        check(bool(cookies["sessionid"]["secure"]) and bool(cookies["sessionid"]["httponly"]),
            "session cookie Secure and HttpOnly flags")
        status, _, body = request("/")
        check(status == 200 and username in body, "authenticated dashboard")
        status, _, _ = request("/admin/")
        check(status == 200, "superuser administration page")
        with tempfile.NamedTemporaryFile(dir=settings.MEDIA_ROOT, prefix="verify-", delete=True) as handle:
            handle.write(b"volume write test")
            handle.flush()
        check(True, "media volume writable by application user")
        status, _, _ = request("/accounts/logout/", {"csrfmiddlewaretoken": cookies["csrftoken"].value})
        check(status == 302, "POST logout")
        status, _, _ = request("/")
        check(status == 302, "logout invalidates session")
    finally:
        user.delete()
    print("All production HTTP smoke checks passed; temporary account removed.")
