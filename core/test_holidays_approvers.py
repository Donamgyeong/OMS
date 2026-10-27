from datetime import date
from unittest.mock import patch
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from .models import ApprovalSettings, Holiday, HolidaySource, HolidaySyncState, User
from .test_workflow import WorkflowFixture
from . import services
from .holidays import SyncError, sync_holidays, event_dates
from .forms import MemberForm, HolidayForm


class ApproverTests(WorkflowFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.root = User.objects.create_user('root', is_superuser=True, department=self.department, onboarding_completed=True)

    def test_roles_and_explicit_override(self):
        self.assertEqual(services.resolve_approver(self.employee), self.manager)
        self.assertEqual(services.resolve_approver(self.manager), self.root)
        self.assertEqual(services.resolve_approver(self.root), self.root)
        self.manager.designated_approver = self.outsider
        self.assertEqual(services.resolve_approver(self.manager), self.outsider)
        self.assertIn(self.root, MemberForm(instance=self.root).fields['designated_approver'].queryset)
        self.assertNotIn(self.employee, MemberForm(instance=self.employee).fields['designated_approver'].queryset)

    def test_multiple_and_inactive_administrator(self):
        User.objects.create_user('root2', is_superuser=True)
        with self.assertRaises(ValidationError):
            services.resolve_approver(self.manager)
        ApprovalSettings.objects.create(default_administrator=self.root)
        self.assertEqual(services.resolve_approver(self.manager), self.root)
        self.root.is_active = False
        self.root.save()
        with self.assertRaises(ValidationError):
            services.resolve_approver(self.manager)

    def test_self_approval_server_and_ui(self):
        self.submit(16, user=self.root)
        plan = self.submit(1, user=self.root)
        self.assertEqual(plan.approver, self.root)
        self.client.force_login(self.root)
        response = self.client.get(reverse('plan_detail', args=[plan.pk]))
        self.assertTrue(response.context['can_decide'])
        self.assertContains(self.client.get(reverse('approvals')), plan.title)
        self.assertEqual(services.transition_plan(self.root, plan.pk, 'approve', plan.version).status, 'approved')

    def test_preview_settings_and_sync_permissions(self):
        self.client.force_login(self.root)
        response = self.client.get(reverse('approver_preview', args=[self.manager.pk]),
            {'department': self.department.pk, 'role': 'manager'})
        self.assertEqual(response.json()['label'], f'자동 선택: {self.root.display_name}')
        self.assertEqual(self.client.get(reverse('setup_edit', args=['approval', 1])).status_code, 200)
        self.assertContains(self.client.get(reverse('setup_edit', args=['member', self.employee.pk])), 'approver-preview')
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse('approver_preview', args=[self.manager.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse('holiday_sync')).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('setup_edit', args=['approval', 1])).status_code, 403)
        self.assertEqual(self.client.get(reverse('holiday_sync')).status_code, 405)

    def test_demoted_self_approver_is_denied(self):
        self.submit(16, user=self.root)
        plan = self.submit(1, user=self.root)
        self.root.is_superuser = False
        self.root.save()
        with self.assertRaises(PermissionDenied):
            services.transition_plan(self.root, plan.pk, 'approve', plan.version)


@override_settings(HOLIDAY_MAILBOX='holidays@example.com', HOLIDAY_CALENDAR_ID='calendar')
class HolidayTests(TestCase):
    def event(self, **kwargs):
        return dict(id='e1', subject='회사 휴일', isAllDay=True,
            start={'dateTime': '2026-11-02T00:00:00', 'timeZone': 'Asia/Seoul'},
            end={'dateTime': '2026-11-04T00:00:00', 'timeZone': 'Asia/Seoul'}, **kwargs)

    @patch('core.holidays.fetch_events')
    def test_idempotence_changes_and_manual_preservation(self, fetch):
        fetch.return_value = [self.event(), self.event()]
        Holiday.objects.create(date=date(2026, 11, 2), name='수동')
        self.assertEqual(sync_holidays(), 2)
        sync_holidays()
        self.assertEqual(HolidaySource.objects.count(), 2)
        self.assertEqual(Holiday.objects.get(date=date(2026, 11, 2)).name, '수동')
        imported = Holiday.objects.get(date=date(2026, 11, 3))
        form = HolidayForm({'date': imported.date, 'name': '직접 수정'}, instance=imported)
        self.assertTrue(form.is_valid())
        form.save()
        fetch.return_value = []
        sync_holidays()
        self.assertEqual(Holiday.objects.count(), 2)
        self.assertEqual(HolidaySource.objects.count(), 0)

    @patch('core.holidays.fetch_events')
    def test_failure_preserves_previous_snapshot_and_releases_lock(self, fetch):
        fetch.return_value = [self.event()]
        sync_holidays()
        fetch.side_effect = SyncError('Microsoft 연결 실패')
        with self.assertRaises(SyncError):
            sync_holidays()
        self.assertEqual(Holiday.objects.count(), 2)
        state = HolidaySyncState.objects.get(pk=1)
        self.assertIsNone(state.running_until)
        self.assertTrue(state.last_error)
        self.assertIsNotNone(state.last_success)

    def test_cancelled_and_non_holidays_and_invalid_dates(self):
        self.assertEqual(event_dates([self.event(isCancelled=True)], date(2026, 1, 1), date(2027, 1, 1)), {})
        event = self.event()
        event['start']['dateTime'] = 'invalid'
        with self.assertRaises(SyncError):
            event_dates([event], date(2026, 1, 1), date(2027, 1, 1))

    @override_settings(HOLIDAY_TENANT_ID='11111111-1111-1111-1111-111111111111',
                       HOLIDAY_CLIENT_ID='client', HOLIDAY_CLIENT_SECRET='secret')
    @patch('core.holidays.request_json')
    def test_all_pages_and_untrusted_next_link(self, request):
        from .holidays import fetch_events
        request.side_effect = [{'access_token': 'token'},
            {'value': [self.event()], '@odata.nextLink': 'https://graph.microsoft.com/v1.0/next'},
            {'value': []}]
        self.assertEqual(len(fetch_events(date(2026, 1, 1), date(2027, 1, 1))), 1)
        request.reset_mock()
        request.side_effect = [{'access_token': 'token'},
            {'value': [], '@odata.nextLink': 'https://evil.example/collect'}]
        with self.assertRaises(SyncError):
            fetch_events(date(2026, 1, 1), date(2027, 1, 1))
        self.assertEqual(request.call_count, 2)

    @patch('core.holidays.fetch_events')
    def test_source_deletion_removes_only_imported_holidays(self, fetch):
        fetch.return_value = [self.event()]
        sync_holidays()
        self.assertEqual(HolidaySyncState.objects.get(pk=1).changed_dates, ['2026-11-02', '2026-11-03'])
        fetch.return_value = []
        sync_holidays()
        self.assertEqual(Holiday.objects.count(), 0)
