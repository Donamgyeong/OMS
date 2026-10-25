"""Development-only HTTP workflow check; removes only its own temporary data."""
import http.cookiejar
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from secrets import token_hex, token_urlsafe
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.conf import settings
from core.models import (ApprovalEvent, Department, Notification, OvertimePlan,
    OvertimePolicy, User, WorkRecord, WorkRecordEvent)

if not settings.DEBUG:
    raise SystemExit("Run only against the isolated development container, with DEBUG enabled.")

BASE = "http://127.0.0.1:8000"


class Session:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))

    def get(self, path):
        return self.opener.open(BASE + path, timeout=10)

    def post(self, path, data, csrf=True):
        values = dict(data)
        if csrf:
            values["csrfmiddlewaretoken"] = next(cookie.value for cookie in self.jar if cookie.name == "csrftoken")
        return self.opener.open(BASE + path, data=urlencode(values).encode(), timeout=10)

    def login(self, username, password):
        self.get("/accounts/login/").close()
        with self.post("/accounts/login/", dict(username=username, password=password)) as response:
            assert response.geturl() == BASE + "/"


tag = token_hex(6)
department = policy = None
users = []
try:
    day = date(2098, 1, 1)
    while OvertimePolicy.objects.filter(effective_from=day).exists():
        day += timedelta(days=1)
    policy = OvertimePolicy.objects.create(effective_from=day, threshold_minutes=960)
    while day.weekday() != 0:
        day += timedelta(days=1)
    department = Department.objects.create(name=f"HTTP 검증 {tag}")
    password = token_urlsafe(24)
    manager = User.objects.create_user(f"smoke_head_{tag}", password=password, department=department, role="manager")
    users.append(manager)
    member = User.objects.create_user(f"smoke_member_{tag}", password=password, department=department)
    users.append(member)
    department.head = manager
    department.save()
    employee_session, manager_session = Session(), Session()
    employee_session.login(member.username, password)
    manager_session.login(manager.username, password)
    with employee_session.get("/health/") as response:
        assert response.status == 200
    with employee_session.get("/static/css/app.css") as response:
        assert response.status == 200
    data = dict(work_date=str(day), end_date="", start_time="00:00", end_time="16:00",
        break_minutes=0, title=f"HTTP 업무 {tag}", reason="검증", intent="submit")
    with employee_session.post("/plans/new/", data) as response:
        assert "/plans/" in response.geturl()
    assert OvertimePlan.objects.get(user=member).status == "confirmed"
    data.update(start_time="18:00", end_time="20:00")
    employee_session.post("/plans/new/", data).close()
    pending = OvertimePlan.objects.get(user=member, status="pending")
    assert pending.submitted_total_minutes == 1080
    with manager_session.get("/approvals/") as response:
        assert data["title"].encode() in response.read()
    employee_session.post("/records/new/", dict(work_date=str(day), started_at=f"{day}T09:00",
        ended_at=f"{day}T20:00", break_minutes=60, plan=pending.pk, description="개발", exception_reason="")).close()
    record = WorkRecord.objects.get(user=member)
    assert record.overtime_minutes == 120 and record.unapproved_at_entry
    decision = dict(version=pending.version, action="approve", comment="HTTP 검증 승인")
    manager_session.post(f"/plans/{pending.pk}/action/", decision).close()
    pending.refresh_from_db()
    assert pending.status == "approved"
    manager_session.post(f"/plans/{pending.pk}/action/", decision).close()
    assert pending.approval_events.filter(result="approved").count() == 1
    with employee_session.get(f"/?month={day:%Y-%m}") as response:
        assert "2시간 0분".encode() in response.read()
    try:
        employee_session.post(f"/plans/{pending.pk}/action/", decision, csrf=False)
        raise AssertionError("Missing CSRF token was accepted")
    except HTTPError as exc:
        assert exc.code == 403
    employee_session.post("/accounts/logout/", {}).close()
    with employee_session.get("/") as response:
        assert "/accounts/login/" in response.geturl()
    print("HTTP workflow passed: login, static, health, exact threshold, pending, inbox, actual, approval, duplicate protection, dashboard, CSRF, logout.")
finally:
    plans = OvertimePlan.objects.filter(user__in=users)
    records = WorkRecord.objects.filter(user__in=users)
    Notification.objects.filter(plan__in=plans).delete()
    ApprovalEvent.objects.filter(plan__in=plans).delete()
    WorkRecordEvent.objects.filter(record__in=records).delete()
    records.delete()
    plans.delete()
    User.objects.filter(pk__in=[user.pk for user in users]).delete()
    if department:
        department.delete()
    if policy:
        policy.delete()
