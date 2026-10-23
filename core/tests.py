from datetime import date, datetime, time, timedelta
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from .models import Department, OvertimePlan, User, WorkRecord
from .selectors import visible_to


class AccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a = Department.objects.create(name="개발")
        cls.b = Department.objects.create(name="인사")
        cls.employee = User.objects.create_user("employee", department=cls.a)
        cls.peer = User.objects.create_user("peer", department=cls.a)
        cls.outsider = User.objects.create_user("outsider", department=cls.b)
        cls.manager = User.objects.create_user("manager", role=User.Role.MANAGER, department=cls.a)
        cls.admin = User.objects.create_user("administrator", role=User.Role.ADMIN)
        for user in [cls.employee, cls.peer, cls.outsider]:
            OvertimePlan.objects.create(user=user, department=user.department, work_date=timezone.localdate(),
                start_time=time(18), end_time=time(20), title="업무", reason="마감")

    def test_scopes(self):
        plans = OvertimePlan.objects.all()
        self.assertEqual(visible_to(plans, self.employee).count(), 1)
        self.assertEqual(visible_to(plans, self.manager).count(), 2)
        self.assertEqual(visible_to(plans, self.admin).count(), 3)

    def test_login_required(self):
        self.assertRedirects(self.client.get("/"), "/accounts/login/?next=/", fetch_redirect_response=False)

    def test_dashboard_does_not_leak_other_department(self):
        self.client.force_login(self.manager)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "peer")
        self.assertNotContains(response, "outsider")

    def test_non_superuser_staff_cannot_open_model_admin(self):
        self.admin.is_staff = True
        self.admin.save()
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/admin/core/user/").status_code, 403)

    def test_duration_calculation_and_invalid_interval(self):
        plan = OvertimePlan.objects.first()
        plan.break_minutes = 30
        plan.save()
        self.assertEqual(plan.duration_minutes, 90)
        plan.end_time = time(17)
        with self.assertRaises(ValidationError):
            plan.save()

    def test_work_record_rejects_another_users_plan(self):
        start = timezone.make_aware(datetime(2026, 10, 23, 9))
        record = WorkRecord(user=self.outsider, department=self.b, work_date=date(2026, 10, 23),
            started_at=start, ended_at=start + timedelta(hours=9),
            plan=OvertimePlan.objects.get(user=self.employee))
        with self.assertRaises(ValidationError):
            record.full_clean()
