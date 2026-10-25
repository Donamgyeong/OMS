"""Trusted session fixtures for business tests; never exposed as an HTTP endpoint."""
from django.test import Client
from django.utils import timezone

from .authentication import BACKEND


class SSOClient(Client):
    def force_login(self, user, backend=BACKEND):
        super().force_login(user, backend=backend)
        session = self.session
        session["entra_authenticated_at"] = timezone.now().timestamp()
        session["entra_auth_version"] = user.auth_version
        session.save()
