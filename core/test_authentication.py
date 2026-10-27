import hashlib
import io
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from joserfc import jwt
from joserfc.jwk import RSAKey
from requests import Response
from requests.exceptions import Timeout

from .authentication import BACKEND, revoke_sessions, state_digest, user_from_claims
from .forms import MemberForm
from .logging import AuthQueryFilter
from .models import Department, EntraIdentity, LoginAttempt, User
from .testing import SSOClient

TENANT = "11111111-1111-4111-8111-111111111111"
CLIENT = "22222222-2222-4222-8222-222222222222"
OBJECT = "33333333-3333-4333-8333-333333333333"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT}"
AUTH_SETTINGS = dict(
    ENTRA_CONFIGURED=True, ENTRA_TENANT_ID=TENANT, ENTRA_CLIENT_ID=CLIENT,
    ENTRA_CLIENT_SECRET="test-secret", ENTRA_AUTHORITY=AUTHORITY,
    ENTRA_REDIRECT_URI="http://testserver/accounts/entra/callback/",
    ENTRA_LOGOUT_MICROSOFT=False,
)


class FakeEntra:
    """Fake only HTTPS transport; real Authlib verifies RSA tokens and OIDC claims."""
    def __init__(self, key):
        self.key = key
        self.codes = {}
        self.token_requests = []
        self.sent_requests = []
        self.override_claims = {}
        self.omit_claims = ()
        self.signing_key = key
        self.no_id_token = False

    def send(self, request, **kwargs):
        self.sent_requests.append((request.url, kwargs.get("timeout")))
        response = Response()
        response.status_code = 200
        response.request = request
        if request.url.endswith("/.well-known/openid-configuration"):
            data = {"issuer": f"{AUTHORITY}/v2.0",
                "authorization_endpoint": f"{AUTHORITY}/oauth2/v2.0/authorize",
                "token_endpoint": f"{AUTHORITY}/oauth2/v2.0/token",
                "jwks_uri": f"{AUTHORITY}/discovery/v2.0/keys",
                "id_token_signing_alg_values_supported": ["RS256"]}
        elif request.url.endswith("/keys"):
            data = {"keys": [self.key.as_dict(private=False)]}
        elif request.url.endswith("/token"):
            params = parse_qs(request.body)
            self.token_requests.append(params)
            nonce = self.codes[params["code"][0]]
            claims = {"iss": f"{AUTHORITY}/v2.0", "aud": CLIENT, "sub": "subject",
                "iat": int(time.time()), "exp": int(time.time()) + 3600,
                "nonce": nonce, "tid": TENANT, "oid": OBJECT,
                "roles": ["OMS.Access"], "name": "테스트 직원", "email": "member@example.com"}
            claims.update(self.override_claims)
            for claim in self.omit_claims:
                claims.pop(claim, None)
            data = {"access_token": "test-access-token", "token_type": "Bearer", "expires_in": 3600}
            if not self.no_id_token:
                data["id_token"] = jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, self.signing_key)
        else:
            raise AssertionError(f"Unexpected network request: {request.url}")
        response._content = json.dumps(data).encode()
        response.headers["Content-Type"] = "application/json"
        return response


@override_settings(**AUTH_SETTINGS)
class EntraFlowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.key = RSAKey.generate_key(2048, parameters={"kid": "test-key"})

    def setUp(self):
        self.provider = FakeEntra(self.key)
        self.transport = patch("requests.sessions.Session.send", side_effect=self.provider.send)
        self.transport.start()
        self.addCleanup(self.transport.stop)

    def begin(self, client=None, destination="/"):
        client = client or self.client
        response = client.get("/accounts/entra/login/", {"next": destination})
        self.assertEqual(response.status_code, 302)
        params = parse_qs(urlsplit(response.url).query)
        code = f"test-code-{len(self.provider.codes)}"
        self.provider.codes[code] = params["nonce"][0]
        return params, "/accounts/entra/callback/?" + urlencode({"state": params["state"][0], "code": code})

    def test_real_oidc_pkce_and_jit_without_storing_tokens(self):
        params, callback = self.begin(destination="/approvals/")
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(set(params["scope"][0].split()), {"openid", "profile", "email"})
        self.assertNotIn("offline_access", params["scope"][0])
        self.assertEqual(params["response_mode"], ["query"])
        self.assertRedirects(self.client.get(callback), "/accounts/onboarding/", fetch_redirect_response=False)
        user = User.objects.get()
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.role, "employee")
        self.assertFalse(user.is_staff or user.is_superuser or user.onboarding_completed)
        identity = user.entra_identity
        self.assertEqual(str(identity.tenant_id), TENANT)
        self.assertEqual(str(identity.object_id), OBJECT)
        token_request = self.provider.token_requests[0]
        self.assertEqual(token_request["client_secret"], ["test-secret"])
        import base64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(token_request["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, params["code_challenge"][0])
        self.assertTrue(all(timeout == 10 for _, timeout in self.provider.sent_requests))
        session = self.client.session
        self.assertEqual(session["_auth_user_backend"], BACKEND)
        self.assertLessEqual(session.get_expiry_age(), 8 * 3600)
        encoded = json.dumps(dict(session))
        for secret in ("id_token", "access_token", "refresh_token", "test-secret", "code_verifier"):
            self.assertNotIn(secret, encoded)
        self.assertFalse(LoginAttempt.objects.exists())

    def test_repeat_login_uses_oid_and_does_not_overwrite_business_rights(self):
        _, callback = self.begin()
        self.client.get(callback)
        user = User.objects.get()
        User.objects.filter(pk=user.pk).update(role="admin", onboarding_completed=True)
        self.client.post("/accounts/logout/")
        self.provider.override_claims = {"email": "changed@example.com", "name": "Changed Name"}
        _, callback = self.begin(destination="/setup/")
        self.assertRedirects(self.client.get(callback), "/setup/", fetch_redirect_response=False)
        self.assertEqual(User.objects.count(), 1)
        user.refresh_from_db()
        self.assertEqual(user.role, "admin")
        self.assertFalse(user.is_superuser)

    def test_invalid_signed_claims_are_rejected_without_provisioning(self):
        for changes in ({"nonce": "wrong"}, {"aud": "wrong"}, {"iss": "https://wrong.example"},
                {"exp": int(time.time()) - 3600}, {"nbf": int(time.time()) + 3600},
                {"tid": "44444444-4444-4444-8444-444444444444"}, {"oid": "not-a-guid"},
                {"roles": []}, {"roles": "OMS.Access"}):
            with self.subTest(changes=changes):
                self.provider.override_claims = changes
                _, callback = self.begin()
                self.assertIn(self.client.get(callback).status_code, (400, 403))
                self.assertFalse(User.objects.exists())
                self.assertNotIn("_auth_user_id", self.client.session)

    def test_missing_claims_and_missing_id_token_are_rejected(self):
        for claim in ("nonce", "exp", "iat", "sub", "iss", "aud", "oid", "tid"):
            with self.subTest(claim=claim):
                self.provider.omit_claims = (claim,)
                _, callback = self.begin()
                self.assertIn(self.client.get(callback).status_code, (400, 403))
                self.assertFalse(User.objects.exists())
        self.provider.omit_claims = ()
        self.provider.no_id_token = True
        _, callback = self.begin()
        self.assertEqual(self.client.get(callback).status_code, 400)
        self.assertFalse(User.objects.exists())

    def test_signature_tampering_is_rejected(self):
        self.provider.signing_key = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
        _, callback = self.begin()
        self.assertEqual(self.client.get(callback).status_code, 400)
        self.assertFalse(User.objects.exists())

    def test_unknown_rotated_key_refreshes_jwks(self):
        # The published key rotates between the initial and forced key fetch.
        from core.authentication import entra_client
        client = entra_client()
        old = RSAKey.generate_key(2048, parameters={"kid": "old-key"})
        client.server_metadata["jwks"] = {"keys": [old.as_dict(private=False)]}
        _, callback = self.begin()
        with patch("core.auth_views.entra_client", return_value=client):
            self.assertEqual(self.client.get(callback).status_code, 302)
        self.assertTrue(User.objects.exists())

    def test_state_cross_browser_expiry_and_replay(self):
        params, callback = self.begin()
        self.assertEqual(Client().get(callback).status_code, 400)
        self.assertTrue(LoginAttempt.objects.exists())
        LoginAttempt.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.get(callback).status_code, 400)
        self.assertFalse(User.objects.exists())
        _, callback = self.begin()
        self.assertEqual(self.client.get(callback).status_code, 302)
        self.assertEqual(Client().get(callback).status_code, 400)
        # An authenticated pending account is redirected before callback processing.
        self.client.post("/accounts/logout/")
        self.assertEqual(self.client.get(callback).status_code, 400)
        self.assertEqual(len(self.provider.token_requests), 1)

    def test_cancel_and_transport_failure_do_not_expose_details(self):
        params, _ = self.begin()
        response = self.client.get("/accounts/entra/callback/", {
            "state": params["state"][0], "error": "access_denied", "error_description": "sensitive-detail"})
        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, "sensitive-detail", status_code=400)
        _, callback = self.begin()
        with patch("requests.sessions.Session.send", side_effect=Timeout("sensitive-secret")):
            response = self.client.get(callback)
        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, "sensitive-secret", status_code=400)
        self.assertFalse(User.objects.exists())

    def test_missing_email_is_allowed_and_email_does_not_link_accounts(self):
        self.provider.omit_claims = ("email",)
        _, callback = self.begin()
        self.assertEqual(self.client.get(callback).status_code, 302)
        self.client.post("/accounts/logout/")
        self.provider.override_claims = {"oid": "55555555-5555-4555-8555-555555555555"}
        _, callback = self.begin()
        self.assertEqual(self.client.get(callback).status_code, 302)
        self.assertEqual(User.objects.count(), 2)

    def test_inactive_account_is_not_reactivated(self):
        _, callback = self.begin()
        self.client.get(callback)
        user = User.objects.get()
        revoke_sessions(user.pk, deactivate=True)
        self.client.post("/accounts/logout/")
        _, callback = self.begin()
        self.assertEqual(self.client.get(callback).status_code, 403)
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_next_rejects_external_redirects_and_auth_loops(self):
        for target in ("https://evil.example", "//evil.example", "/accounts/login/", "http://testserver/admin/login/"):
            with self.subTest(target=target):
                params, _ = self.begin(destination=target)
                self.assertEqual(self.client.session[f"entra_next_{params['state'][0]}"], "/")

    def test_multiple_tabs_keep_independent_nonce_and_destination(self):
        # Preconfigured user can enter either tab's destination after authentication.
        user = user_from_claims({"tid": TENANT, "oid": OBJECT, "roles": ["OMS.Access"]})
        User.objects.filter(pk=user.pk).update(role="admin", onboarding_completed=True)
        _, first = self.begin(destination="/approvals/")
        _, second = self.begin(destination="/setup/")
        self.assertRedirects(self.client.get(second), "/setup/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get(first), "/approvals/", fetch_redirect_response=False)
        self.assertEqual(User.objects.count(), 1)

    def test_abandoned_tab_flows_are_bounded_and_oldest_is_rejected(self):
        _, oldest = self.begin()
        for _ in range(6):
            self.begin()
        session = self.client.session
        self.assertEqual(len([key for key in session.keys() if key.startswith("_state_entra_")]), 5)
        self.assertEqual(len([key for key in session.keys() if key.startswith("entra_next_")]), 5)
        self.assertEqual(LoginAttempt.objects.count(), 5)
        self.assertEqual(self.client.get(oldest).status_code, 400)

    @override_settings(SESSION_COOKIE_SECURE=True)
    def test_login_rotates_session_and_sets_secure_http_only_cookie(self):
        _, callback = self.begin()
        before = self.client.session.session_key
        response = self.client.get(callback, secure=True)
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(before, self.client.session.session_key)
        self.assertTrue(response.cookies["sessionid"]["secure"])
        self.assertTrue(response.cookies["sessionid"]["httponly"])
        self.assertEqual(response.cookies["sessionid"]["samesite"], "Lax")


@override_settings(**AUTH_SETTINGS)
class EntraAccessTests(TestCase):
    client_class = SSOClient

    def setUp(self):
        self.user = user_from_claims({"tid": TENANT, "oid": OBJECT, "roles": ["OMS.Access"]})

    def test_pending_user_cannot_get_or_post_business_or_admin_urls(self):
        self.client.force_login(self.user)
        for path in ("/", "/plans/new/", "/plans/1/action/", "/approvals/", "/records/new/", "/setup/", "/setup/member/1/", "/admin/"):
            for method in (self.client.get, self.client.post):
                response = method(path, {})
                self.assertRedirects(response, "/accounts/onboarding/", fetch_redirect_response=False)
        self.assertContains(self.client.get("/accounts/onboarding/"), "업무 설정 대기")
        self.assertNotContains(self.client.get("/accounts/onboarding/"), "계획 등록")
        self.assertEqual(self.client.get("/health/").status_code, 200)
        self.assertEqual(self.client.post("/accounts/logout/").status_code, 302)

    def test_local_password_authentication_and_admin_login_are_disabled(self):
        root = User.objects.create_superuser("local-root", password="Strong-password-998!")
        self.assertIsNone(authenticate(username=root.username, password="Strong-password-998!"))
        self.assertFalse(self.client.login(username=root.username, password="Strong-password-998!"))
        self.assertEqual(self.client.post("/accounts/login/", {"username": root.username, "password": "Strong-password-998!"}).status_code, 405)
        self.assertEqual(self.client.post("/admin/login/", {"username": root.username, "password": "Strong-password-998!"}).status_code, 405)
        self.assertRedirects(self.client.get("/admin/login/"), "/accounts/login/?next=/admin/", fetch_redirect_response=False)

    def test_bootstrap_admin_and_command_revocation(self):
        with self.assertRaises(CommandError):
            call_command("bootstrap_entra_admin", "66666666-6666-4666-8666-666666666666", stdout=io.StringIO())
        self.client.force_login(self.user)
        call_command("bootstrap_entra_admin", OBJECT, superuser=True, stdout=io.StringIO())
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_superuser and self.user.is_staff and self.user.onboarding_completed)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        self.assertEqual(self.client.get("/admin/core/user/add/").status_code, 403)
        call_command("revoke_entra_sessions", OBJECT, deactivate=True, stdout=io.StringIO())
        self.assertRedirects(self.client.get("/"), "/accounts/login/?next=/", fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertEqual(self.user.auth_version, 1)
        with self.assertRaises(CommandError):
            call_command("bootstrap_entra_admin", OBJECT, stdout=io.StringIO())

    def test_revoke_all_sessions_and_reactivation_cannot_restore_them(self):
        other = SSOClient()
        self.client.force_login(self.user)
        other.force_login(self.user)
        revoke_sessions(self.user.pk)
        for client in (self.client, other):
            self.assertRedirects(client.get("/accounts/onboarding/"), "/accounts/login/?next=/accounts/onboarding/", fetch_redirect_response=False)

    def test_absolute_session_expiry_and_missing_session_marker(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["entra_authenticated_at"] = timezone.now().timestamp() - 8 * 3600 - 1
        session.save()
        self.assertEqual(self.client.get("/accounts/onboarding/").status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)
        plain_client = Client()
        plain_client.force_login(self.user)
        self.assertEqual(plain_client.get("/accounts/onboarding/").status_code, 302)
        self.assertNotIn("_auth_user_id", plain_client.session)

    def test_onboarding_requires_business_configuration(self):
        self.user.onboarding_completed = True
        with self.assertRaises(ValidationError):
            self.user.full_clean()
        self.assertNotIn("password", MemberForm(instance=self.user).fields)
        self.assertNotIn("username", MemberForm(instance=self.user).fields)

    def test_administrator_can_complete_onboarding_and_deactivation_revokes_sessions(self):
        administrator = User.objects.create_user("admin", role="admin", onboarding_completed=True)
        head = User.objects.create_user("head")
        department = Department.objects.create(name="개발", head=head)
        User.objects.filter(pk=head.pk).update(department=department)
        self.client.force_login(administrator)
        data = dict(employee_number="E001", department=department.pk, role="employee",
            is_active="on", daily_work_minutes=480, working_weekdays="0,1,2,3,4", onboarding_completed="on")
        response = self.client.post(f"/setup/member/{self.user.pk}/", data)
        self.assertRedirects(response, "/setup/", fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.onboarding_completed)
        member_client = SSOClient()
        member_client.force_login(self.user)
        self.assertEqual(member_client.get("/").status_code, 200)
        data.pop("is_active")
        self.assertEqual(self.client.post(f"/setup/member/{self.user.pk}/", data).status_code, 302)
        data["is_active"] = "on"
        self.assertEqual(self.client.post(f"/setup/member/{self.user.pk}/", data).status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.auth_version, 2)
        self.assertRedirects(member_client.get("/"), "/accounts/login/?next=/", fetch_redirect_response=False)

    def test_logout_requires_post_and_csrf(self):
        client = SSOClient(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.get("/accounts/logout/").status_code, 405)
        self.assertEqual(client.post("/accounts/logout/").status_code, 403)

    def test_https_admin_user_save_preserves_csrf_protection(self):
        administrator = User.objects.create_superuser(
            "csrf-admin", role="admin", onboarding_completed=True,
        )
        client = SSOClient(enforce_csrf_checks=True)
        client.force_login(administrator)
        path = f"/admin/core/user/{self.user.pk}/change/"
        response = client.get(path, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Referrer-Policy"], "same-origin")
        data = dict(
            first_name="수정된 이름", last_name="", email="member@example.com",
            is_active="on", employee_number="", department="", role="employee",
            designated_approver="", daily_work_minutes=480,
            working_weekdays="0,1,2,3,4", _save="저장",
            csrfmiddlewaretoken=client.cookies["csrftoken"].value,
        )
        # Browsers without an Origin header rely on the same-site Referer.
        response = client.post(path, data, secure=True,
            HTTP_REFERER=f"https://testserver{path}")
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "수정된 이름")
        for headers in ({}, {"HTTP_REFERER": "https://evil.example/"},
                {"HTTP_ORIGIN": "https://evil.example"}):
            with self.subTest(headers=headers):
                self.assertEqual(client.post(path, data, secure=True, **headers).status_code, 403)
        data.pop("csrfmiddlewaretoken")
        self.assertEqual(client.post(path, data, secure=True,
            HTTP_REFERER=f"https://testserver{path}").status_code, 403)

    @override_settings(ENTRA_LOGOUT_MICROSOFT=True, ENTRA_POST_LOGOUT_REDIRECT_URI="https://oms.example.com/accounts/login/")
    def test_microsoft_logout_clears_local_session_first(self):
        self.client.force_login(self.user)
        response = self.client.post("/accounts/logout/")
        self.assertTrue(response.url.startswith(f"{AUTHORITY}/oauth2/v2.0/logout?"))
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(ENTRA_CONFIGURED=False)
    def test_unconfigured_app_is_closed_and_deployment_check_fails(self):
        self.assertContains(self.client.get("/accounts/login/"), "준비 중")
        self.assertEqual(self.client.get("/accounts/entra/login/").status_code, 503)
        self.assertEqual(self.client.get("/accounts/entra/callback/").status_code, 503)
        from .checks import entra_deployment_checks
        self.assertEqual(entra_deployment_checks(None)[0].id, "oms.E001")

    def test_callback_query_log_is_redacted(self):
        record = logging.LogRecord("django.server", logging.INFO, "", 1,
            '"GET %s HTTP/1.1" 400 10', ("/accounts/entra/callback/?code=secret-code&state=secret-state",), None)
        AuthQueryFilter().filter(record)
        self.assertNotIn("secret-code", record.getMessage())
        self.assertNotIn("secret-state", record.getMessage())


@override_settings(**AUTH_SETTINGS)
class EntraConcurrencyTests(TransactionTestCase):
    def run_parallel(self, operation):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL concurrency verification required")
        barrier = Barrier(2)

        def worker(_):
            try:
                barrier.wait(timeout=10)
                return operation()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(worker, range(2)))

    def test_parallel_first_login_creates_one_user_and_identity(self):
        claims = {"tid": TENANT, "oid": OBJECT, "roles": ["OMS.Access"]}
        results = self.run_parallel(lambda: user_from_claims(claims).pk)
        self.assertEqual(results[0], results[1])
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(EntraIdentity.objects.count(), 1)

    def test_parallel_callback_consumes_state_only_once(self):
        from .authentication import consume_attempt
        LoginAttempt.objects.create(state_digest=state_digest("single-use-state"),
            expires_at=timezone.now() + timedelta(minutes=10))
        self.assertCountEqual(self.run_parallel(lambda: consume_attempt("single-use-state")), [True, False])
