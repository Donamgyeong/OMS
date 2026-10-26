"""Bot Connector authenticated, personal-scope lifecycle endpoint."""
import json
from functools import lru_cache
from time import time
from uuid import UUID

import requests
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from .models import TeamsConversation
from .teams import valid_service_url

OPENID_KEYS = "https://login.botframework.com/v1/.well-known/keys"


class SigningKeysUnavailable(Exception):
    pass


@lru_cache(maxsize=2)
def signing_keys(hour):
    response = requests.get(OPENID_KEYS, timeout=(5, 10), allow_redirects=False)
    if response.status_code != 200:
        raise SigningKeysUnavailable
    try:
        keys = response.json()
        if not isinstance(keys, dict) or not isinstance(keys.get("keys"), list):
            raise ValueError
        return keys
    except ValueError:
        raise SigningKeysUnavailable from None


def authenticate(request, activity):
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise ValueError("Missing token")
    # Refresh once for key rotation. Only fixed Microsoft keys are trusted.
    for attempt in range(2):
        keys = signing_keys(int(time() // 3600))
        try:
            token = jwt.decode(header[7:], KeySet.import_key_set(keys), algorithms=["RS256"])
            break
        except JoseError:
            if attempt:
                raise
            signing_keys.cache_clear()
    claims = token.claims
    jwt.JWTClaimsRegistry(iss={"essential": True, "value": "https://api.botframework.com"},
        aud={"essential": True, "value": settings.TEAMS_CLIENT_ID}, exp={"essential": True},
        nbf={"essential": True}, serviceurl={"essential": True}).validate(claims)
    if claims.get("serviceurl") != activity.get("serviceUrl") or not valid_service_url(activity.get("serviceUrl", "")):
        raise ValueError("Invalid service URL")
    matching_key = next((key for key in keys["keys"] if key.get("kid") == token.header.get("kid")), None)
    if matching_key is None or ("endorsements" in matching_key and "msteams" not in matching_key["endorsements"]):
        raise ValueError("Invalid channel endorsement")


@csrf_exempt
@require_POST
def messages(request):
    if not settings.TEAMS_ENABLED:
        return HttpResponse(status=404)
    if len(request.body) > 65536:
        return HttpResponse(status=413)
    try:
        activity = json.loads(request.body)
        if not isinstance(activity, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=400)
    try:
        authenticate(request, activity)
    except (requests.RequestException, SigningKeysUnavailable):
        return HttpResponse(status=503)
    except (JoseError, ValueError, TypeError, KeyError, AttributeError):
        return HttpResponse(status=401)
    try:
        conversation = activity.get("conversation", {})
        tenant_id = UUID(activity.get("channelData", {}).get("tenant", {}).get("id", ""))
        if (activity.get("channelId") != "msteams" or str(tenant_id) != settings.TEAMS_TENANT_ID
                or conversation.get("conversationType") != "personal"):
            return HttpResponse(status=403)
        sender = activity.get("from", {})
        # Sender aadObjectId is the initiating user, never a display-name/email match.
        object_id = UUID(sender.get("aadObjectId", ""))
        conversation_id = conversation["id"]
        if not isinstance(conversation_id, str) or not 0 < len(conversation_id) <= 512:
            raise ValueError
        service_url = activity["serviceUrl"]
        if len(service_url) > 512:
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        return HttpResponse(status=400)
    lookup = dict(tenant_id=tenant_id, object_id=object_id, bot_id=settings.TEAMS_CLIENT_ID)
    kind = activity.get("type")
    if kind == "installationUpdate" and activity.get("action") == "remove":
        TeamsConversation.objects.filter(**lookup, conversation_id=conversation_id).update(active=False)
    elif kind in ("installationUpdate", "conversationUpdate", "message"):
        if kind == "installationUpdate" and activity.get("action") not in ("add", "add-upgrade", "remove-upgrade"):
            return JsonResponse({})
        # A removal must not recreate the binding; reinstall or a message restores it.
        if kind == "conversationUpdate" and activity.get("membersRemoved"):
            TeamsConversation.objects.filter(**lookup, conversation_id=conversation_id).update(active=False)
        else:
            TeamsConversation.objects.update_or_create(**lookup, defaults={
                "conversation_id": conversation_id, "service_url": service_url, "active": True})
    return JsonResponse({})
