from datetime import date, datetime, time
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from .forms import PlanForm, RecordForm
from .test_workflow import WorkflowFixture


class InputTests(WorkflowFixture, TestCase):
    def test_split_attendance_preserves_overnight_dates(self):
        data = dict(work_date='2026-11-02', started_at_0='2026-11-02', started_at_1='22:15',
                    ended_at_0='2026-11-03', ended_at_1='07:45', break_minutes=60,
                    description='야간 운영', exception_reason='긴급 대응')
        form = RecordForm(data, user=self.employee)
        self.assertTrue(form.is_valid(), form.errors)
        start = timezone.localtime(form.cleaned_data['started_at'])
        end = timezone.localtime(form.cleaned_data['ended_at'])
        self.assertEqual(start.date(), date(2026, 11, 2))
        self.assertEqual(start.time(), time(22, 15))
        self.assertEqual(end.date(), date(2026, 11, 3))
        self.assertEqual(end.time(), time(7, 45))

    def test_partial_datetime_has_errors_without_losing_entered_value(self):
        form = RecordForm(dict(work_date='2026-11-02', started_at_0='2026-11-02',
            started_at_1='22:15', ended_at_0='2026-11-03', ended_at_1='', break_minutes=0,
            description='운영'), user=self.employee)
        self.assertFalse(form.is_valid())
        self.assertIn('ended_at', form.errors)
        self.assertIn('value="22:15"', str(form['started_at']))
        self.assertIn('value="2026-11-03"', str(form['ended_at']))

    def test_existing_dates_and_minutes_render_for_editing(self):
        plan = self.submit(2)
        html = PlanForm(instance=plan).as_p()
        self.assertIn('value="2026-11-02"', html)
        self.assertIn('value="00:00"', html)
        record = RecordForm(user=self.employee, initial={'started_at': timezone.make_aware(datetime(2026, 11, 2, 9, 35))})
        self.assertIn('value="09:35"', str(record['started_at']))
        self.assertIn('value="2026-11-02"', str(record['started_at']))

    def test_legacy_datetime_submission_and_redisplay(self):
        data = dict(work_date='2026-11-02', started_at='2026-11-02T09:00',
            ended_at='2026-11-02T18:00', break_minutes=60, description='개발')
        form = RecordForm(data, user=self.employee)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn('value="09:00"', str(form['started_at']))

    def test_form_and_sections_render_on_application_pages(self):
        self.client.force_login(self.employee)
        for name in ('plan_create', 'record_create'):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'class="form-section"', count=2)
        self.assertContains(self.client.get(reverse('record_create')), 'name="started_at_0"')
        self.assertContains(self.client.get(reverse('approvals')), 'class="content-section"', count=2)
