"""Transactional Teams outbox and bounded Bot Connector REST transport."""
from datetime import timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit
from uuid import uuid4

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import EntraIdentity, OvertimePlan, TeamsConversation, TeamsDelivery

MAX_ATTEMPTS = 6
LEASE_SECONDS = 120


def valid_service_url(value):
    try:
        uri = urlsplit(value)
        return (uri.scheme == "https" and uri.hostname in settings.TEAMS_SERVICE_HOSTS
                and uri.port in (None, 443) and not any((uri.username, uri.password, uri.query, uri.fragment)))
    except (ValueError, TypeError):
        return False


def enqueue(entry, plan, actor):
    if not settings.TEAMS_ENABLED:
        return
    if entry.result == OvertimePlan.Status.PENDING:
        recipient = plan.approver
        kind = "requested"
    elif entry.result in (OvertimePlan.Status.APPROVED, OvertimePlan.Status.REJECTED):
        recipient = plan.user
        kind = entry.result
    else:
        return
    TeamsDelivery.objects.get_or_create(event=entry, recipient=recipient, kind=kind, defaults={
        "payload": {"title": plan.title, "applicant": plan.user.get_full_name() or plan.user.username,
                    "actor": actor.get_full_name() or actor.username, "work_date": str(plan.work_date),
                    "duration_minutes": plan.duration_minutes, "version": plan.version}})


def card_activity(delivery):
    data = delivery.payload
    label = {"requested": "결재 요청", "approved": "승인 완료", "rejected": "반려"}[delivery.kind]
    return {"type": "message", "text": f"[OMS] {label}: {data['title']}",
        "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
            "body": [{"type": "TextBlock", "text": f"OMS · {label}", "weight": "Bolder", "wrap": True},
                     {"type": "TextBlock", "text": data["title"], "wrap": True},
                     {"type": "FactSet", "facts": [
                         {"title": "신청자", "value": data["applicant"]},
                         {"title": "근무 예정일", "value": data["work_date"]},
                         {"title": "계획 시간", "value": f"{data['duration_minutes']}분"},
                         {"title": "처리자", "value": data["actor"]},
                         {"title": "계획 버전", "value": str(data["version"])}]}],
            "actions": [{"type": "Action.OpenUrl", "title": "OMS에서 확인",
                         "url": settings.OMS_PUBLIC_URL + reverse("plan_detail", args=[delivery.event.plan_id])}]}}]}


class DeliveryError(Exception):
    def __init__(self, code, *, retry=False, delay=None):
        self.code, self.retry, self.delay = code, retry, delay
        super().__init__(code)


def retry_after(value):
    try:
        return max(1, int(value))
    except (ValueError, TypeError):
        try:
            return max(1, int((parsedate_to_datetime(value) - timezone.now()).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return None


class Connector:
    """Single-tenant client credentials; never forward bearer tokens on redirects."""
    def __init__(self):
        self.access_token = None
        self.expires_at = timezone.now()

    def request(self, url, **kwargs):
        try:
            response = requests.post(url, timeout=(5, 15), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise DeliveryError("network_error", retry=True) from None
        if not 200 <= response.status_code < 300:
            raise DeliveryError(f"http_{response.status_code}",
                retry=response.status_code in (408, 429) or response.status_code >= 500,
                delay=retry_after(response.headers.get("Retry-After")))
        try:
            return response.json()
        except ValueError:
            raise DeliveryError("invalid_response", retry=True) from None

    def token(self):
        if not self.access_token or timezone.now() >= self.expires_at:
            data = self.request(f"https://login.microsoftonline.com/{settings.TEAMS_TENANT_ID}/oauth2/v2.0/token", data={
                "grant_type": "client_credentials", "client_id": settings.TEAMS_CLIENT_ID,
                "client_secret": settings.TEAMS_CLIENT_SECRET, "scope": "https://api.botframework.com/.default"})
            try:
                self.access_token = data["access_token"]
                if not isinstance(self.access_token, str) or not self.access_token:
                    raise ValueError
                self.expires_at = timezone.now() + timedelta(seconds=max(1, int(data["expires_in"]) - 60))
            except (KeyError, ValueError, TypeError):
                raise DeliveryError("invalid_token_response", retry=True) from None
        return self.access_token

    def send(self, conversation, activity):
        if not valid_service_url(conversation.service_url):
            raise DeliveryError("invalid_service_url")
        url = (conversation.service_url.rstrip("/") + "/v3/conversations/"
               + quote(conversation.conversation_id, safe="") + "/activities")
        token = self.token()
        try:
            data = self.request(url, json=activity, headers={"Authorization": f"Bearer {token}"})
        except DeliveryError as error:
            if error.code == "http_401":
                self.access_token = None
            raise
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
            raise DeliveryError("invalid_message_response", retry=True)
        return data["id"][:512]


@transaction.atomic
def claim_delivery():
    now = timezone.now()
    due = Q(status=TeamsDelivery.Status.PENDING, next_attempt_at__lte=now)
    expired = Q(status=TeamsDelivery.Status.PROCESSING, lease_until__lte=now)
    item = TeamsDelivery.objects.select_for_update(skip_locked=True).filter(due | expired).order_by("pk").first()
    if item is None:
        return None
    # Crashes count towards the bounded retry budget, too.
    if item.attempts >= MAX_ATTEMPTS:
        item.status = TeamsDelivery.Status.FAILED
        item.error_code = "lease_expired"
        item.lease_until = item.lease_token = None
        item.save()
        return item
    item.status = TeamsDelivery.Status.PROCESSING
    item.attempts += 1
    item.lease_until = now + timedelta(seconds=LEASE_SECONDS)
    item.lease_token = uuid4()
    item.save()
    return item


def finish(item, **changes):
    # An expired worker cannot overwrite the result of the next lease holder.
    TeamsDelivery.objects.filter(pk=item.pk, status=TeamsDelivery.Status.PROCESSING,
        lease_token=item.lease_token).update(lease_until=None, lease_token=None, **changes)


def process_one(connector=None):
    if not settings.TEAMS_ENABLED:
        return False
    item = claim_delivery()
    if item is None:
        return False
    if item.status != TeamsDelivery.Status.PROCESSING:
        return True
    try:
        lease_token = item.lease_token
        item = TeamsDelivery.objects.select_related("event__plan", "recipient").get(pk=item.pk)
        if item.lease_token != lease_token:
            return True
        plan = item.event.plan
        if (not item.recipient.is_active or not item.recipient.onboarding_completed
                or (item.kind == "requested" and (plan.status != OvertimePlan.Status.PENDING
                    or plan.version != item.payload["version"] or plan.approver_id != item.recipient_id))):
            finish(item, status=TeamsDelivery.Status.SKIPPED, error_code="stale_or_inactive")
            return True
        identity = EntraIdentity.objects.filter(user_id=item.recipient_id,
            tenant_id=settings.TEAMS_TENANT_ID).first()
        if not identity:
            raise DeliveryError("identity_missing")
        conversation = TeamsConversation.objects.filter(tenant_id=identity.tenant_id,
            object_id=identity.object_id, bot_id=settings.TEAMS_CLIENT_ID, active=True).first()
        if not conversation:
            raise DeliveryError("app_not_installed")
        activity_id = (connector or Connector()).send(conversation, card_activity(item))
        finish(item, status=TeamsDelivery.Status.SENT, activity_id=activity_id,
               sent_at=timezone.now(), error_code="")
    except DeliveryError as error:
        status = TeamsDelivery.Status.BLOCKED
        if error.retry:
            status = TeamsDelivery.Status.PENDING if item.attempts < MAX_ATTEMPTS else TeamsDelivery.Status.FAILED
        finish(item, status=status, error_code=error.code,
            next_attempt_at=timezone.now() + timedelta(seconds=error.delay or min(3600, 30 * 2 ** (item.attempts - 1))))
    except Exception:
        # No raw exception messages: HTTP libraries can include secrets and URLs.
        finish(item, status=TeamsDelivery.Status.FAILED, error_code="internal_error")
        raise
    return True
