"""Entra-only authentication and server-side account/session management."""
import hashlib
import logging
from uuid import UUID, uuid4

from authlib.integrations.django_client import OAuth
from django.conf import settings
from django.contrib.auth.backends import BaseBackend
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from .models import EntraIdentity, LoginAttempt, User

logger = logging.getLogger("oms.authentication")
BACKEND = "core.authentication.EntraBackend"


class EntraBackend(BaseBackend):
    # No password authenticate method: even local users and superusers cannot use it.
    def get_user(self, user_id):
        return User.objects.filter(pk=user_id, is_active=True).first()


def entra_client():
    oauth = OAuth()
    return oauth.register(
        "entra",
        client_id=settings.ENTRA_CLIENT_ID,
        client_secret=settings.ENTRA_CLIENT_SECRET,
        server_metadata_url=f"{settings.ENTRA_AUTHORITY}/v2.0/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid profile email",
            "code_challenge_method": "S256",
            "token_endpoint_auth_method": "client_secret_post",
            "default_timeout": 10,
        },
    )


def state_digest(state):
    return hashlib.sha256(state.encode()).hexdigest()


def consume_attempt(state):
    # Atomic DELETE makes the callback single-use even for concurrent requests.
    deleted, _ = LoginAttempt.objects.filter(
        state_digest=state_digest(state), expires_at__gt=timezone.now()
    ).delete()
    return bool(deleted)


def user_from_claims(claims):
    """Only call with signature/issuer/audience/nonce-validated ID token claims."""
    try:
        tenant_id = UUID(claims["tid"])
        object_id = UUID(claims["oid"])
    except (ValueError, TypeError, KeyError, AttributeError):
        raise PermissionDenied("회사 계정 식별자를 확인할 수 없습니다.") from None
    roles = claims.get("roles", [])
    if str(tenant_id) != settings.ENTRA_TENANT_ID or not isinstance(roles, list) or settings.ENTRA_ACCESS_ROLE not in roles:
        raise PermissionDenied("OMS 이용 권한이 없는 회사 계정입니다. 관리자에게 문의하세요.")

    # The identity constraint resolves simultaneous first logins without orphan users.
    try:
        with transaction.atomic():
            identity = EntraIdentity.objects.select_related("user").filter(
                tenant_id=tenant_id, object_id=object_id
            ).first()
            if identity:
                user = identity.user
            else:
                user = User(username=f"entra_{uuid4().hex}",
                    first_name=str(claims.get("name") or "")[:150],
                    email=str(claims.get("email") or "")[:254])
                user.set_unusable_password()
                user.save()
                EntraIdentity.objects.create(user=user, tenant_id=tenant_id, object_id=object_id)
                logger.info("account_created user_id=%s", user.pk)
    except IntegrityError:
        identity = EntraIdentity.objects.select_related("user").filter(
            tenant_id=tenant_id, object_id=object_id
        ).first()
        if identity is None:
            raise
        user = identity.user
    if not user.is_active:
        raise PermissionDenied("이용이 중지된 계정입니다. 관리자에게 문의하세요.")
    return user


def revoke_sessions(user_id, *, deactivate=False):
    updates = {"auth_version": F("auth_version") + 1}
    if deactivate:
        updates["is_active"] = False
    User.objects.filter(pk=user_id).update(**updates)
    logger.info("sessions_revoked user_id=%s deactivated=%s", user_id, deactivate)
