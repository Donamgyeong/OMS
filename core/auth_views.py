import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from authlib.integrations.base_client.errors import OAuthError
from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST
from joserfc.errors import JoseError
from requests.exceptions import RequestException

from .authentication import BACKEND, consume_attempt, entra_client, state_digest, user_from_claims
from .models import LoginAttempt

logger = logging.getLogger("oms.authentication")


def safe_next(request, value):
    if value and url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        # Prevent auth endpoint loops even when next is an absolute same-host URL.
        from urllib.parse import urlsplit
        if not urlsplit(value).path.startswith("/accounts/") and not urlsplit(value).path.startswith("/admin/login"):
            return value
    return reverse("dashboard")


def auth_error(request, message, status=400):
    return render(request, "registration/login.html", {"auth_error": message, "configured": settings.ENTRA_CONFIGURED}, status=status)


@never_cache
@require_GET
def login_page(request):
    if request.user.is_authenticated:
        return redirect(safe_next(request, request.GET.get("next")))
    return render(request, "registration/login.html", {
        "configured": settings.ENTRA_CONFIGURED,
        "next": safe_next(request, request.GET.get("next")),
    })


@never_cache
@require_GET
@sensitive_variables()
def entra_login(request):
    if not settings.ENTRA_CONFIGURED:
        return auth_error(request, "회사 계정 로그인을 준비 중입니다. 관리자에게 문의하세요.", 503)
    if request.user.is_authenticated:
        return redirect(safe_next(request, request.GET.get("next")))
    # Bound abandoned per-tab flows and their destinations in the server session.
    flow_keys = [key for key in request.session.keys() if key.startswith("_state_entra_")]
    for key in flow_keys:
        if request.session[key].get("exp", 0) <= timezone.now().timestamp():
            request.session.pop(key, None)
            request.session.pop(f"entra_next_{key.removeprefix('_state_entra_')}", None)
    flow_keys = [key for key in request.session.keys() if key.startswith("_state_entra_")]
    for key in flow_keys[:-4]:
        old_state = key.removeprefix("_state_entra_")
        request.session.pop(key, None)
        request.session.pop(f"entra_next_{old_state}", None)
        LoginAttempt.objects.filter(state_digest=state_digest(old_state)).delete()
    state = secrets.token_urlsafe(32)
    client = entra_client()
    try:
        response = client.authorize_redirect(request, settings.ENTRA_REDIRECT_URI, state=state, response_mode="query")
    except (OAuthError, RequestException, ValueError, JoseError):
        logger.warning("login_start_failed")
        return auth_error(request, "Microsoft 로그인에 연결할 수 없습니다. 잠시 후 다시 시도하세요.", 503)
    # Keep destinations per state to support multiple simultaneous browser tabs.
    flow_key = f"_state_entra_{state}"
    flow = request.session[flow_key]
    flow["exp"] = (timezone.now() + timedelta(minutes=10)).timestamp()
    request.session[flow_key] = flow
    request.session[f"entra_next_{state}"] = safe_next(request, request.GET.get("next"))
    LoginAttempt.objects.filter(expires_at__lte=timezone.now()).delete()
    LoginAttempt.objects.create(state_digest=state_digest(state), expires_at=timezone.now() + timedelta(minutes=10))
    return response


@never_cache
@require_GET
@sensitive_variables()
def entra_callback(request):
    if not settings.ENTRA_CONFIGURED:
        return auth_error(request, "회사 계정 로그인을 준비 중입니다.", 503)
    state = request.GET.get("state", "")
    # Check session binding before consuming a globally single-use login attempt.
    state_key = f"_state_entra_{state}"
    if not state or len(state) > 200 or state_key not in request.session or not consume_attempt(state):
        logger.warning("login_state_rejected")
        return auth_error(request, "로그인 요청이 만료되었거나 올바르지 않습니다. 다시 로그인하세요.")
    destination = request.session.pop(f"entra_next_{state}", reverse("dashboard"))
    if request.GET.get("error"):
        request.session.pop(state_key, None)
        logger.info("login_cancelled")
        return auth_error(request, "Microsoft 로그인이 취소되었거나 허용되지 않았습니다. 다시 시도하세요.")
    try:
        token = entra_client().authorize_access_token(
            request,
            claims_options={
                "iss": {"essential": True, "value": f"{settings.ENTRA_AUTHORITY}/v2.0"},
                "aud": {"essential": True, "value": settings.ENTRA_CLIENT_ID},
                "exp": {"essential": True},
                "iat": {"essential": True},
                "sub": {"essential": True},
            },
        )
        claims = token.get("userinfo")
        if not token.get("id_token") or not claims:
            raise ValueError("ID token missing")
        user = user_from_claims(claims)
    except PermissionDenied as exc:
        logger.warning("login_access_denied")
        return auth_error(request, str(exc), 403)
    except (OAuthError, RequestException, ValueError, TypeError, KeyError, JoseError):
        logger.warning("login_validation_failed")
        return auth_error(request, "회사 계정 로그인을 확인할 수 없습니다. 다시 로그인하세요.")
    finally:
        request.session.pop(state_key, None)
    login(request, user, backend=BACKEND)
    now = timezone.now()
    request.session["entra_authenticated_at"] = now.timestamp()
    request.session["entra_auth_version"] = user.auth_version
    request.session.set_expiry(now + timedelta(seconds=settings.ENTRA_SESSION_SECONDS))
    logger.info("login_success user_id=%s", user.pk)
    if not user.onboarding_completed:
        return redirect("onboarding")
    return redirect(safe_next(request, destination))


@never_cache
@login_required
@require_GET
def onboarding(request):
    if request.user.onboarding_completed:
        return redirect("dashboard")
    return render(request, "registration/onboarding.html")


@never_cache
@require_POST
def logout_view(request):
    user_id = request.user.pk if request.user.is_authenticated else None
    logout(request)
    logger.info("logout user_id=%s", user_id)
    if settings.ENTRA_LOGOUT_MICROSOFT and settings.ENTRA_CONFIGURED:
        return redirect(f"{settings.ENTRA_AUTHORITY}/oauth2/v2.0/logout?" + urlencode({
            "post_logout_redirect_uri": settings.ENTRA_POST_LOGOUT_REDIRECT_URI,
        }))
    return redirect("login")
