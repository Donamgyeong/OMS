import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections, transaction
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from joserfc import jwt
from joserfc.jwk import RSAKey
from requests import Response
from requests.exceptions import Timeout

from . import services
from .checks import teams_checks
from .models import EntraIdentity, TeamsConversation, TeamsDelivery
from .teams import Connector, DeliveryError, claim_delivery, finish, process_one
from .test_workflow import WorkflowFixture

TENANT = "11111111-1111-4111-8111-111111111111"
BOT = "22222222-2222-4222-8222-222222222222"
OBJECT = "33333333-3333-4333-8333-333333333333"
SERVICE = "https://smba.trafficmanager.net/amer/"
SETTINGS = dict(TEAMS_ENABLED=True, TEAMS_TENANT_ID=TENANT, TEAMS_CLIENT_ID=BOT,
    TEAMS_CLIENT_SECRET="test-bot-secret", OMS_PUBLIC_URL="https://testserver",
    ENTRA_TENANT_ID=TENANT)


@override_settings(**SETTINGS)
class TeamsWorkflowTests(WorkflowFixture, TestCase):
    def pending(self):
        self.submit(16)
        return self.submit(1)

    def bind(self, user):
        identity = EntraIdentity.objects.create(user=user, tenant_id=TENANT, object_id=uuid4())
        return TeamsConversation.objects.create(tenant_id=TENANT, object_id=identity.object_id,
            bot_id=BOT, conversation_id="a:personal-conversation", service_url=SERVICE)

    def test_recipients_and_resubmit_are_distinct(self):
        plan = self.pending()
        requested = TeamsDelivery.objects.get()
        self.assertEqual(requested.recipient, self.manager)
        plan = services.transition_plan(self.manager, plan.pk, "reject", plan.version, "기밀 의견")
        rejected = TeamsDelivery.objects.get(kind="rejected")
        self.assertEqual(rejected.recipient, self.employee)
        self.assertNotIn("기밀 의견", json.dumps(rejected.payload))
        plan = services.save_plan(self.employee, self.data(1), pk=plan.pk, version=plan.version, submit=True)
        self.assertEqual(TeamsDelivery.objects.filter(kind="requested").count(), 2)
        plan = services.transition_plan(self.manager, plan.pk, "approve", plan.version)
        self.assertEqual(TeamsDelivery.objects.get(kind="approved").recipient, self.employee)
        with self.assertRaises(ValidationError):
            services.transition_plan(self.manager, plan.pk, "approve", plan.version)
        self.assertEqual(TeamsDelivery.objects.count(), 4)

    def test_rollback_leaves_no_delivery(self):
        self.submit(16)
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.submit(1)
                raise RuntimeError("rollback")
        self.assertFalse(TeamsDelivery.objects.exists())

    @override_settings(TEAMS_ENABLED=False)
    def test_disabled_never_queues_or_sends(self):
        self.pending()
        self.assertFalse(TeamsDelivery.objects.exists())
        connector = Mock()
        self.assertFalse(process_one(connector))
        connector.send.assert_not_called()

    def test_success_snapshot_and_no_duplicate_poll(self):
        plan = self.pending()
        self.bind(self.manager)
        connector = Mock()
        connector.send.return_value = "activity-1"
        self.assertTrue(process_one(connector))
        self.assertFalse(process_one(connector))
        delivery = TeamsDelivery.objects.get()
        self.assertEqual(delivery.status, "sent")
        self.assertEqual(delivery.activity_id, "activity-1")
        activity = connector.send.call_args.args[1]
        self.assertIn(f"https://testserver/plans/{plan.pk}/", json.dumps(activity))
        self.assertNotIn("고객사 납기", json.dumps(activity, ensure_ascii=False))
        self.assertEqual(delivery.attempts, 1)

    def test_missing_install_and_manual_retry(self):
        self.pending()
        process_one(Mock())
        delivery = TeamsDelivery.objects.get()
        self.assertEqual(delivery.error_code, "identity_missing")
        self.bind(self.manager)
        TeamsDelivery.objects.filter(pk=delivery.pk).update(status="pending", next_attempt_at=timezone.now())
        connector = Mock()
        connector.send.return_value = "sent-after-install"
        process_one(connector)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "sent")

    def test_stale_request_and_disabled_user_are_skipped(self):
        plan = self.pending()
        services.transition_plan(self.employee, plan.pk, "recall", plan.version)
        connector = Mock()
        process_one(connector)
        self.assertEqual(TeamsDelivery.objects.get().status, "skipped")
        connector.send.assert_not_called()

    def test_429_retries_then_exhausts(self):
        self.pending()
        self.bind(self.manager)
        connector = Mock()
        connector.send.side_effect = DeliveryError("http_429", retry=True, delay=120)
        now = timezone.now()
        process_one(connector)
        delivery = TeamsDelivery.objects.get()
        self.assertEqual(delivery.status, "pending")
        self.assertGreaterEqual(delivery.next_attempt_at, now + timedelta(seconds=120))
        self.assertFalse(process_one(connector))
        TeamsDelivery.objects.update(attempts=5, next_attempt_at=now)
        process_one(connector)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "failed")
        self.assertEqual(delivery.attempts, 6)

    def test_expired_lease_and_old_owner_cannot_finish(self):
        self.pending()
        old = claim_delivery()
        self.assertIsNone(claim_delivery())
        TeamsDelivery.objects.update(lease_until=timezone.now() - timedelta(seconds=1))
        current = claim_delivery()
        self.assertNotEqual(old.lease_token, current.lease_token)
        finish(old, status="sent")
        current.refresh_from_db()
        self.assertEqual(current.status, "processing")
        finish(current, status="sent")
        current.refresh_from_db()
        self.assertEqual(current.status, "sent")

    def test_max_crashes_do_not_loop_forever(self):
        self.pending()
        old = claim_delivery()
        TeamsDelivery.objects.update(attempts=6, lease_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(claim_delivery().status, "failed")
        self.assertIsNone(claim_delivery())

    def test_403_requires_operator_intervention(self):
        self.pending()
        self.bind(self.manager)
        connector = Mock()
        connector.send.side_effect = DeliveryError("http_403")
        process_one(connector)
        self.assertEqual(TeamsDelivery.objects.get().status, "blocked")

    def test_admin_retry_preserves_sent_and_logs_change(self):
        from django.contrib import admin
        from django.contrib.admin.models import LogEntry
        from .admin import TeamsDeliveryAdmin
        from .models import User
        plan = self.pending()
        services.transition_plan(self.manager, plan.pk, "approve", plan.version)
        TeamsDelivery.objects.filter(kind="requested").update(status="sent")
        TeamsDelivery.objects.filter(kind="approved").update(status="blocked", attempts=3, error_code="app_not_installed")
        root = User.objects.create_superuser("teams-root", password=None)
        request = RequestFactory().post("/admin/")
        request.user = root
        model_admin = TeamsDeliveryAdmin(TeamsDelivery, admin.site)
        model_admin.retry_failed(request, TeamsDelivery.objects.all())
        retried = TeamsDelivery.objects.get(kind="approved")
        self.assertEqual(retried.status, "pending")
        self.assertEqual(retried.attempts, 0)
        self.assertEqual(TeamsDelivery.objects.get(kind="requested").status, "sent")
        self.assertTrue(LogEntry.objects.filter(user=root, object_id=str(retried.pk)).exists())
        request.user = self.admin
        self.assertFalse(model_admin.has_module_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))

    def test_app_missing_and_wrong_bot_are_not_used(self):
        self.pending()
        identity = EntraIdentity.objects.create(user=self.manager, tenant_id=TENANT, object_id=uuid4())
        TeamsConversation.objects.create(tenant_id=TENANT, object_id=identity.object_id, bot_id=uuid4(),
            conversation_id="wrong-bot", service_url=SERVICE)
        connector = Mock()
        process_one(connector)
        self.assertEqual(TeamsDelivery.objects.get().error_code, "app_not_installed")
        connector.send.assert_not_called()

    def test_duplicate_enqueue_is_idempotent(self):
        from .teams import enqueue
        plan = self.pending()
        entry = plan.approval_events.get()
        enqueue(entry, plan, self.employee)
        self.assertEqual(TeamsDelivery.objects.count(), 1)


def response(data, status=200, headers=None):
    result = Response()
    result.status_code = status
    result._content = json.dumps(data).encode()
    result.headers.update(headers or {})
    return result


@override_settings(**SETTINGS)
class TeamsEndpointTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.key = RSAKey.generate_key(2048, parameters={"kid": "bot-key"})

    def setUp(self):
        from .teams_views import signing_keys
        signing_keys.cache_clear()
        self.addCleanup(signing_keys.cache_clear)
        self.keys = patch("core.teams_views.requests.get", return_value=response({"keys": [self.key.as_dict(private=False)]}))
        self.keys.start()
        self.addCleanup(self.keys.stop)
        self.client = Client(enforce_csrf_checks=True)
        self.activity = {"type": "installationUpdate", "action": "add", "channelId": "msteams",
            "serviceUrl": SERVICE, "channelData": {"tenant": {"id": TENANT}},
            "conversation": {"id": "personal-conversation", "conversationType": "personal"},
            "from": {"id": "29:sender", "aadObjectId": OBJECT}}

    def post(self, activity=None, claims=None, key=None):
        activity = self.activity if activity is None else activity
        data = {"iss": "https://api.botframework.com", "aud": BOT, "nbf": int(time.time()) - 10,
            "exp": int(time.time()) + 600, "serviceurl": activity["serviceUrl"]}
        data.update(claims or {})
        token = jwt.encode({"alg": "RS256", "kid": "bot-key"}, data, key or self.key)
        return self.client.post("/api/teams/messages/", json.dumps(activity), content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + token)

    def test_signed_install_uninstall_reinstall_and_message(self):
        self.assertEqual(self.post().status_code, 200)
        item = TeamsConversation.objects.get()
        self.assertEqual(item.object_id, UUID(OBJECT))
        self.assertTrue(item.active)
        self.activity["action"] = "remove"
        self.assertEqual(self.post().status_code, 200)
        item.refresh_from_db()
        self.assertFalse(item.active)
        self.activity.update(type="message", text="hello")
        self.assertEqual(self.post().status_code, 200)
        item.refresh_from_db()
        self.assertTrue(item.active)
        self.assertEqual(TeamsConversation.objects.count(), 1)

    def test_invalid_claims_signature_and_service_are_denied(self):
        for claims in ({"iss": "https://attacker.test"}, {"aud": str(uuid4())},
                {"exp": int(time.time()) - 3600}, {"nbf": int(time.time()) + 3600},
                {"serviceurl": "https://attacker.test"}):
            with self.subTest(claims=claims):
                self.assertEqual(self.post(claims=claims).status_code, 401)
        wrong_key = RSAKey.generate_key(2048, parameters={"kid": "bot-key"})
        self.assertEqual(self.post(key=wrong_key).status_code, 401)
        self.activity["serviceUrl"] = "https://attacker.test/"
        self.assertEqual(self.post().status_code, 401)
        self.assertFalse(TeamsConversation.objects.exists())

    def test_missing_claims_are_denied(self):
        for field in ("exp", "nbf", "aud", "iss", "serviceurl"):
            self.assertEqual(self.post(claims={field: None}).status_code, 401)

    def test_wrong_tenant_and_shared_scope_denied(self):
        self.activity["channelData"]["tenant"]["id"] = str(uuid4())
        self.assertEqual(self.post().status_code, 403)
        self.activity["channelData"]["tenant"]["id"] = TENANT
        self.activity["conversation"]["conversationType"] = "channel"
        self.assertEqual(self.post().status_code, 403)
        self.assertFalse(TeamsConversation.objects.exists())

    def test_without_token_cannot_register_or_delete(self):
        self.assertEqual(self.client.post("/api/teams/messages/", json.dumps(self.activity),
            content_type="application/json").status_code, 401)
        self.assertFalse(TeamsConversation.objects.exists())

    @override_settings(TEAMS_ENABLED=False)
    def test_disabled_endpoint_hidden(self):
        self.assertEqual(self.post().status_code, 404)

    def test_invalid_body_and_method(self):
        self.assertEqual(self.client.get("/api/teams/messages/").status_code, 405)
        self.assertEqual(self.client.post("/api/teams/messages/", "{", content_type="application/json").status_code, 400)

    def test_microsoft_key_service_failure_returns_retryable_status(self):
        with patch("core.teams_views.requests.get", return_value=response({}, 503)):
            self.assertEqual(self.post().status_code, 503)

    def test_repeated_install_is_idempotent_and_old_uninstall_does_not_remove_new_chat(self):
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(TeamsConversation.objects.count(), 1)
        self.activity["conversation"]["id"] = "new-conversation"
        self.assertEqual(self.post().status_code, 200)
        self.activity["conversation"]["id"] = "personal-conversation"
        self.activity["action"] = "remove"
        self.assertEqual(self.post().status_code, 200)
        self.assertTrue(TeamsConversation.objects.get().active)


@override_settings(**SETTINGS)
class ConnectorTests(TestCase):
    def test_credentials_cache_and_encoded_conversation_path(self):
        connector = Connector()
        conversation = TeamsConversation(service_url=SERVICE, conversation_id="a:b/c")
        with patch("core.teams.requests.post", side_effect=[response({"access_token": "token", "expires_in": 3600}),
                response({"id": "one"}), response({"id": "two"})]) as post:
            self.assertEqual(connector.send(conversation, {}), "one")
            self.assertEqual(connector.send(conversation, {}), "two")
        self.assertEqual(post.call_count, 3)
        self.assertIn("a%3Ab%2Fc/activities", post.call_args.args[0])
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertEqual(post.call_args_list[0].kwargs["data"]["scope"], "https://api.botframework.com/.default")

    def test_timeout_and_retry_after(self):
        connector = Connector()
        with patch("core.teams.requests.post", side_effect=Timeout):
            with self.assertRaises(DeliveryError) as caught:
                connector.token()
        self.assertTrue(caught.exception.retry)
        with patch("core.teams.requests.post", return_value=response({}, 429, {"Retry-After": "90"})):
            with self.assertRaises(DeliveryError) as caught:
                connector.token()
        self.assertEqual(caught.exception.delay, 90)

    def test_untrusted_service_receives_no_request(self):
        with patch("core.teams.requests.post") as post:
            with self.assertRaises(DeliveryError):
                Connector().send(TeamsConversation(service_url="https://attacker.test", conversation_id="x"), {})
        post.assert_not_called()

    def test_configuration_validation(self):
        self.assertEqual(teams_checks(None), [])
        with override_settings(TEAMS_CLIENT_ID="", TEAMS_CLIENT_SECRET="", OMS_PUBLIC_URL="http://testserver"):
            self.assertEqual({e.id for e in teams_checks(None)}, {"oms.E002", "oms.E003", "oms.E004"})


@override_settings(**SETTINGS)
class WorkerCommandTests(TransactionTestCase):
    def test_worker_drain_and_disabled_guard(self):
        call_command("send_teams_notifications", once=True)
        with override_settings(TEAMS_ENABLED=False):
            with self.assertRaises(CommandError):
                call_command("send_teams_notifications", once=True)


@override_settings(**SETTINGS)
class TeamsConcurrencyTests(WorkflowFixture, TransactionTestCase):
    def test_two_workers_claim_only_once(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL row locks required")
        self.submit(16)
        self.submit(1)
        barrier = Barrier(2)

        def worker():
            try:
                barrier.wait(timeout=10)
                item = claim_delivery()
                return item.pk if item else None
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(TeamsDelivery.objects.get().attempts, 1)
