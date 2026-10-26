from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, logout
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.cache import patch_cache_control

from .authentication import BACKEND


class EntraSessionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            authenticated_at = request.session.get("entra_authenticated_at")
            valid = (
                request.session.get(BACKEND_SESSION_KEY) == BACKEND
                and isinstance(authenticated_at, (int, float))
                and 0 <= timezone.now().timestamp() - authenticated_at < settings.ENTRA_SESSION_SECONDS
                and request.session.get("entra_auth_version") == request.user.auth_version
            )
            if not valid:
                logout(request)
        response = self.get_response(request)
        if request.user.is_authenticated:
            patch_cache_control(response, private=True, no_store=True)
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not request.user.is_authenticated or request.user.onboarding_completed:
            return None
        # Route names, not URL prefixes: pending users cannot reach business/admin URLs.
        if request.resolver_match.url_name not in {"onboarding", "logout", "health", "teams_messages"}:
            return redirect("onboarding")
        return None
