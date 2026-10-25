from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from threading import Barrier
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from .models import Department, Holiday, Notification, OvertimePlan, OvertimePolicy, User, WorkRecord
from . import services


class WorkflowFixture:
    def setUp(self):
        self.department = Department.objects.create(name="개발")
        self.manager = User.objects.create_user("head", role=User.Role.MANAGER, department=self.department)
        self.department.head = self.manager
        self.department.save()
        self.employee = User.objects.create_user("member", department=self.department)
        self.other_department = Department.objects.create(name="외부")
        self.outsider = User.objects.create_user("outside", department=self.other_department)
        self.admin = User.objects.create_user("viewer", role=User.Role.ADMIN)
        self.policy = OvertimePolicy.objects.create(effective_from=date(2020, 1, 1), threshold_minutes=960)

    def data(self, hours=2, day=date(2026, 11, 2)):
        return dict(work_date=day, end_date=None, start_time=time(0), end_time=time(hours),
            break_minutes=0, title="출시 준비", reason="고객사 납기")

    def submit(self, hours=2, user=None, **kwargs):
        return services.save_plan(user or self.employee, self.data(hours, **kwargs), submit=True)

    def record_data(self, plan=None, hours=11, day=date(2026, 11, 2)):
        start = timezone.make_aware(datetime.combine(day, time(9)))
        return dict(work_date=day, started_at=start, ended_at=start + timedelta(hours=hours),
            break_minutes=60, plan=plan, description="개발 및 배포", exception_reason="긴급 장애 처리")


class WorkflowTests(WorkflowFixture, TestCase):
    def test_exact_threshold_and_pending_included(self):
        self.assertEqual(self.submit(14).status, "confirmed")
        self.assertEqual(self.submit(2).status, "confirmed")
        pending = self.submit(1)
        self.assertEqual(pending.status, "pending")
        self.assertEqual(pending.approver, self.manager)
        self.assertEqual(self.submit(1).submitted_total_minutes, 1080)
        self.assertEqual(Notification.objects.filter(user=self.manager).count(), 2)

    def test_draft_rejected_and_cancelled_do_not_count(self):
        services.save_plan(self.employee, self.data(15))
        initial = self.submit(16)
        rejected = self.submit(1)
        services.transition_plan(self.manager, rejected.pk, "reject", rejected.version, "업무 조정")
        self.assertEqual(self.submit(1).submitted_total_minutes, 1020)
        services.transition_plan(self.employee, initial.pk, "cancel", initial.version)
        self.assertEqual(self.submit(1).submitted_total_minutes, 120)
        rejected.refresh_from_db()
        self.assertEqual(rejected.status, "rejected")

    def test_reject_requires_comment_and_decision_cannot_repeat(self):
        self.submit(16)
        plan = self.submit(1)
        with self.assertRaises(ValidationError):
            services.transition_plan(self.manager, plan.pk, "reject", plan.version)
        services.transition_plan(self.manager, plan.pk, "approve", plan.version, "진행")
        with self.assertRaises(ValidationError):
            services.transition_plan(self.manager, plan.pk, "approve", plan.version)
        self.assertEqual(plan.approval_events.filter(result="approved").count(), 1)

    def test_admin_viewer_and_owner_cannot_approve(self):
        self.submit(16)
        plan = self.submit(1)
        for user in (self.employee, self.admin, self.outsider):
            with self.assertRaises(PermissionDenied):
                services.transition_plan(user, plan.pk, "approve", plan.version)

    def test_manager_self_submission_uses_designated_approver(self):
        self.submit(16, user=self.manager)
        with self.assertRaises(ValidationError):
            self.submit(1, user=self.manager)
        self.manager.designated_approver = self.outsider
        self.manager.save()
        plan = self.submit(1, user=self.manager)
        self.assertEqual(plan.approver, self.outsider)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("plan_detail", args=[plan.pk])).status_code, 200)

    def test_recall_edit_resubmit_and_history(self):
        self.submit(16)
        plan = self.submit(1)
        with self.assertRaises(ValidationError):
            services.save_plan(self.employee, self.data(2), pk=plan.pk, version=plan.version, submit=True)
        plan = services.transition_plan(self.employee, plan.pk, "recall", plan.version)
        changed = services.save_plan(self.employee, self.data(2), pk=plan.pk, version=plan.version, submit=True)
        self.assertEqual(changed.submitted_total_minutes, 1080)
        self.assertEqual(changed.approval_events.count(), 3)
        with self.assertRaises(ValidationError):
            services.save_plan(self.employee, self.data(3), pk=plan.pk, version=1, submit=True)

    def test_edit_excludes_original_and_policy_is_frozen(self):
        plan = self.submit(16)
        OvertimePolicy.objects.create(effective_from=date(2026, 11, 1), threshold_minutes=60)
        plan.refresh_from_db()
        self.assertEqual(plan.threshold_snapshot, 960)
        changed = services.save_plan(self.employee, self.data(2), pk=plan.pk, version=plan.version, submit=True)
        self.assertEqual(changed.submitted_total_minutes, 120)
        self.assertEqual(changed.status, "pending")
        self.assertEqual(changed.approval_events.last().snapshot["before"]["threshold_snapshot"], 960)
        self.assertEqual(changed.approval_events.last().snapshot["before"]["duration_minutes"], 960)

    def test_month_boundary_break_allocation_and_new_month_policy(self):
        data = self.data(2, date(2026, 10, 31))
        data.update(start_time=time(23), end_date=date(2026, 11, 1), break_minutes=30)
        OvertimePolicy.objects.create(effective_from=date(2026, 11, 1), threshold_minutes=30)
        plan = services.save_plan(self.employee, data, submit=True)
        self.assertEqual(plan.duration_minutes, 150)
        self.assertEqual(services.monthly_minutes(plan), {"2026-10": 60, "2026-11": 90})
        self.assertEqual(plan.status, "pending")
        self.assertEqual(plan.monthly_snapshot["2026-11"]["threshold"], 30)

    def test_missing_policy_or_approver_rolls_back(self):
        self.policy.delete()
        with self.assertRaises(ValidationError):
            self.submit()
        self.assertFalse(OvertimePlan.objects.exists())
        services.save_plan(self.employee, self.data())
        self.assertEqual(OvertimePlan.objects.count(), 1)

    def test_department_and_approver_snapshot_survive_transfer(self):
        self.submit(16)
        plan = self.submit(1)
        self.employee.department = self.other_department
        self.employee.save()
        self.department.head = self.outsider
        self.department.save()
        changed = services.transition_plan(self.manager, plan.pk, "approve", plan.version)
        self.assertEqual(changed.department, self.department)
        self.assertEqual(changed.approver, self.manager)

    def test_actual_calculation_link_difference_and_revision(self):
        plan = self.submit(3)
        record = services.save_record(self.employee, self.record_data(plan))
        self.assertEqual(record.duration_minutes, 600)
        self.assertEqual(record.overtime_minutes, 120)
        data = self.record_data(plan, hours=13)
        with self.assertRaises(ValidationError):
            services.save_record(self.employee, data, pk=record.pk, version=record.version)
        record = services.save_record(self.employee, data, pk=record.pk, version=record.version, change_reason="퇴근 시각 정정")
        self.assertEqual(record.overtime_minutes, 240)
        self.assertEqual(record.events.count(), 2)
        services.transition_plan(self.employee, plan.pk, "cancel", plan.version)
        record.refresh_from_db()
        self.assertEqual(record.plan_id, plan.pk)

    def test_unplanned_and_excess_require_reason(self):
        data = self.record_data()
        data["exception_reason"] = ""
        with self.assertRaises(ValidationError):
            services.save_record(self.employee, data)
        plan = self.submit(1)
        data["plan"] = plan
        with self.assertRaises(ValidationError):
            services.save_record(self.employee, data)

    def test_pending_plan_does_not_block_actual(self):
        self.submit(16)
        plan = self.submit(2)
        record = services.save_record(self.employee, self.record_data(plan))
        self.assertTrue(record.unapproved_at_entry)

    def test_holiday_work_and_daily_duplicate(self):
        Holiday.objects.create(date=date(2026, 11, 2), name="창립기념일")
        record = services.save_record(self.employee, self.record_data())
        self.assertEqual(record.overtime_minutes, 600)
        with self.assertRaises(ValidationError):
            services.save_record(self.employee, self.record_data())

    def test_overnight_actual_and_cross_user_link(self):
        data = self.record_data(hours=20, day=date(2026, 10, 31))
        record = services.save_record(self.employee, data)
        self.assertEqual(set(record.daily_minutes), {"2026-10-31", "2026-11-01"})
        data = self.record_data(self.submit(user=self.outsider))
        with self.assertRaises(ValidationError):
            services.save_record(self.employee, data)

    def test_staff_forms_and_decision_flow(self):
        self.client.force_login(self.employee)
        for path in ("/", "/plans/new/", "/records/new/", "/approvals/"):
            self.assertEqual(self.client.get(path).status_code, 200)
        data = dict(work_date="2026-11-02", end_date="", start_time="00:00", end_time="16:00",
            break_minutes="0", title="웹 신청", reason="마감", intent="submit")
        response = self.client.post("/plans/new/", data)
        self.assertEqual(response.status_code, 302)
        plan = OvertimePlan.objects.get()
        self.assertEqual(self.client.get(response.url).status_code, 200)
        data.update(end_time="01:00", intent="preview")
        response = self.client.post("/plans/new/", data)
        self.assertContains(response, "승인 대기")
        self.assertEqual(OvertimePlan.objects.count(), 1)
        data["intent"] = "submit"
        self.client.post("/plans/new/", data)
        plan = OvertimePlan.objects.get(status="pending")
        self.client.force_login(self.manager)
        self.assertContains(self.client.get("/approvals/"), "웹 신청")
        response = self.client.post(reverse("plan_action", args=[plan.pk]), {"version": plan.version, "action": "approve", "comment": "확인"})
        self.assertEqual(response.status_code, 302)
        plan.refresh_from_db()
        self.assertEqual(plan.status, "approved")

    def test_record_form_and_invalid_inputs(self):
        self.client.force_login(self.employee)
        data = dict(work_date="2026-11-02", started_at="2026-11-02T09:00", ended_at="2026-11-02T20:00",
            break_minutes="60", plan="", description="배포", exception_reason="긴급", version="")
        response = self.client.post("/records/new/", data)
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(response.url), "2시간 0분")
        record = WorkRecord.objects.get()
        self.assertEqual(self.client.get(reverse("record_edit", args=[record.pk])).status_code, 200)
        data.update(ended_at="2026-11-02T18:00", change_reason="정정", version=record.version)
        response = self.client.post(reverse("record_edit", args=[record.pk]), data)
        self.assertEqual(response.status_code, 302)
        record.refresh_from_db()
        self.assertEqual(record.overtime_minutes, 0)
        self.assertEqual(self.client.post("/plans/new/", {"start_time": "10:00", "end_time": "12:00"}).status_code, 200)

    def test_no_cross_department_or_unauthorized_edits(self):
        plan = self.submit()
        record = services.save_record(self.employee, self.record_data(plan))
        self.client.force_login(self.outsider)
        for path in (reverse("plan_detail", args=[plan.pk]), reverse("plan_edit", args=[plan.pk]),
                     reverse("record_detail", args=[record.pk]), reverse("record_edit", args=[record.pk])):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("plan_action", args=[plan.pk])).status_code, 405)

    def test_configuration_access_and_user_creation(self):
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get("/setup/").status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/setup/").status_code, 200)
        response = self.client.post("/setup/member/new/", dict(username="newmember", password="Strong-local-778!",
            department=self.department.pk, role="employee", is_active="on", daily_work_minutes=480,
            working_weekdays="0,1,2,3,4", is_superuser="on", is_staff="on"))
        self.assertEqual(response.status_code, 302)
        member = User.objects.get(username="newmember")
        self.assertTrue(member.check_password("Strong-local-778!"))
        self.assertFalse(member.is_staff)
        self.assertFalse(member.is_superuser)
        from django.contrib.admin.models import LogEntry
        self.assertTrue(LogEntry.objects.filter(user=self.admin).exists())
        for kind in ("member", "department", "policy", "holiday"):
            self.assertEqual(self.client.get(f"/setup/{kind}/new/").status_code, 200)
        root = User.objects.create_superuser("root", password="root-password")
        self.assertEqual(self.client.post(f"/setup/member/{root.pk}/", {}).status_code, 403)

    def test_department_cycles_and_weak_password_are_rejected(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.post(f"/setup/department/{self.department.pk}/", dict(
            name="개발", parent=self.department.pk, head=self.manager.pk, active="on")), "순환")
        self.assertContains(self.client.post("/setup/member/new/", dict(username="weak", password="123",
            role="employee", daily_work_minutes=480, working_weekdays="0,1,2,3,4")), "errorlist")
        self.assertFalse(User.objects.filter(username="weak").exists())

    def test_department_transfer_does_not_leak_new_department_records(self):
        old = self.submit()
        self.employee.department = self.other_department
        self.employee.save()
        data = self.data()
        data["title"] = "새 부서 비공개 업무"
        services.save_plan(self.employee, data, submit=True)
        self.client.force_login(self.manager)
        self.assertNotContains(self.client.get(reverse("plan_detail", args=[old.pk])), "새 부서 비공개 업무")

    def test_retrospective_approval_is_flagged_in_actual(self):
        self.submit(16, day=date(2020, 11, 2))
        plan = self.submit(2, day=date(2020, 11, 2))
        services.transition_plan(self.manager, plan.pk, "approve", plan.version)
        plan.refresh_from_db()
        self.assertTrue(plan.is_retrospective)
        record = services.save_record(self.employee, self.record_data(plan, day=date(2020, 11, 2)))
        self.assertTrue(record.unapproved_at_entry)

    def test_csrf_and_filter_do_not_change_own_remaining(self):
        from django.test import Client
        self.submit(2)
        self.client.force_login(self.employee)
        response = self.client.get("/?month=2026-11&status=rejected")
        self.assertEqual(response.context["own_total"], 120)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.employee)
        self.assertEqual(csrf_client.post("/plans/new/", {}).status_code, 403)

    def test_legacy_record_migration_preserves_manual_overtime(self):
        from django.apps import apps
        from importlib import import_module
        from types import SimpleNamespace
        data = self.record_data(hours=10)
        record = WorkRecord.objects.create(user=self.employee, department=self.department,
            work_date=data["work_date"], started_at=data["started_at"], ended_at=data["ended_at"],
            break_minutes=60, overtime_minutes=75)
        WorkRecord.objects.filter(pk=record.pk).update(duration_minutes=0, daily_minutes={})
        migration = import_module("core.migrations.0003_preserve_existing_work_totals")
        migration.preserve_totals(apps, SimpleNamespace(connection=connection))
        record.refresh_from_db()
        self.assertEqual(record.duration_minutes, 540)
        self.assertEqual(record.overtime_minutes, 75)
        self.assertEqual(sum(v["overtime"] for v in record.daily_minutes.values()), 75)


class ConcurrencyTests(WorkflowFixture, TransactionTestCase):
    def test_parallel_submissions_cannot_bypass_threshold(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL row locks required")
        self.submit(15)
        barrier = Barrier(2)
        def worker():
            try:
                user = User.objects.get(pk=self.employee.pk)
                barrier.wait(timeout=10)
                return self.submit(1, user=user).status
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        self.assertCountEqual(results, ["confirmed", "pending"])

    def test_parallel_decisions_only_one_succeeds(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL row locks required")
        self.submit(16)
        plan = self.submit(1)
        barrier = Barrier(2)
        def worker():
            try:
                actor = User.objects.get(pk=self.manager.pk)
                barrier.wait(timeout=10)
                try:
                    services.transition_plan(actor, plan.pk, "approve", plan.version)
                    return "approved"
                except ValidationError:
                    return "stale"
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(lambda _: worker(), range(2))), ["approved", "stale"])
        self.assertEqual(plan.approval_events.filter(result="approved").count(), 1)
