"""Server-side HTTP test fixture. Does not exercise or bypass the public SSO flow.

Requires the same trusted database access as manage.py; has no HTTP endpoint.
"""
from datetime import timedelta

from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.utils import timezone

from core.authentication import BACKEND


def create_session(user):
    session = SessionStore()
    now = timezone.now()
    session[SESSION_KEY] = str(user.pk)
    session[BACKEND_SESSION_KEY] = BACKEND
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    session["entra_authenticated_at"] = now.timestamp()
    session["entra_auth_version"] = user.auth_version
    session.set_expiry(now + timedelta(hours=8))
    session.create()
    return session
