from datetime import date, datetime, time, timedelta
from io import BytesIO
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook
from . import services
from .models import OvertimePlan, WorkRecord, ReportAllowance, ReportDownload, User
from .reporting import build_report, export_xlsx
from .test_workflow import WorkflowFixture


class ReportingTests(WorkflowFixture, TestCase):
    def record(self, overtime, day=date(2026, 11, 2), department=None, user=None, plan=None):
        owner = user or self.employee
        started = timezone.make_aware(datetime.combine(day, time(8)))
        return WorkRecord.objects.create(user=owner, department=department or owner.department,
            work_date=day, started_at=started, ended_at=started + timedelta(minutes=480+overtime),
            duration_minutes=480+overtime, overtime_minutes=overtime, break_minutes=0, plan=plan,
            daily_minutes={str(day): {'worked': 480+overtime, 'standard': 480, 'overtime': overtime}},
            description='개발')

    def report(self, user=None, start=date(2026, 11, 1), end=date(2026, 11, 1), **kwargs):
        return build_report(user or self.admin, start, end, **kwargs)

    def test_threshold_boundaries_and_numeric_excel_values(self):
        for extra in (0, 960, 961, 1500):
            WorkRecord.objects.all().delete()
            # Direct snapshot fixture: long totals need multiple legitimate daily slices.
            first = min(extra, 900)
            self.record(first)
            if extra > first:
                self.record(extra-first, day=date(2026, 11, 3))
            report = self.report()
            row = report['rows'][0]
            self.assertEqual(row['overtime'], extra)
            self.assertEqual(row['extra'], max(extra-960, 0))
            self.assertEqual(row['included'], min(extra, 960))
        workbook = load_workbook(BytesIO(export_xlsx(report)), data_only=False)
        self.assertEqual(len(workbook.sheetnames), 4)
        sheet = workbook.worksheets[0]
        self.assertEqual(sheet.freeze_panes, 'E2')
        headers = {cell.value: cell.column for cell in sheet[1]}
        self.assertEqual(sheet.cell(2, headers['실제 초과 근무(분)']).value, 1500)
        self.assertEqual(sheet.cell(2, headers['포함 기준 초과분(분)']).value, 540)

    def test_months_do_not_offset_each_other(self):
        self.record(600, day=date(2026, 10, 2))
        self.record(660, day=date(2026, 11, 2))
        self.record(660, day=date(2026, 11, 3))
        report = self.report(start=date(2026, 10, 1))
        self.assertEqual([r['extra'] for r in report['rows']], [0, 360])
        self.assertEqual(report['total_extra'], 360)

    def test_stored_cross_month_daily_minutes_and_exclusive_end(self):
        record = self.record(120)
        record.work_date = date(2026, 10, 31)
        record.started_at = timezone.make_aware(datetime(2026, 10, 31, 22))
        record.ended_at = timezone.make_aware(datetime(2026, 11, 1, 8))
        record.daily_minutes = {'2026-10-31': {'worked':120,'standard':0,'overtime':120},
                                '2026-11-01': {'worked':480,'standard':0,'overtime':480}}
        record.overtime_minutes = 600
        record.save()
        report = self.report(start=date(2026, 10, 1))
        self.assertEqual([r['overtime'] for r in report['rows']], [120,480])
        # Changing today's attendance policy must not recalculate historical snapshots.
        self.employee.daily_work_minutes = 1
        self.employee.save()
        self.assertEqual(self.report(start=date(2026,10,1))['rows'][0]['overtime'], 120)

    def test_partial_department_scope_hides_monthly_extra(self):
        self.record(900)
        self.record(300, day=date(2026, 11, 3), department=self.other_department)
        row = self.report(user=self.manager)['rows'][0]
        self.assertEqual(row['overtime'], 900)
        self.assertIsNone(row['extra'])
        self.assertNotIn('외부', row['departments'])
        self.assertEqual(self.report()['rows'][0]['extra'], 240)
        self.assertIsNone(self.report(department=self.department)['rows'][0]['extra'])
        self.assertEqual(self.report(user=self.employee)['rows'][0]['extra'], 240)

    def test_legacy_cross_month_is_not_guessed(self):
        record = self.record(120)
        WorkRecord.objects.filter(pk=record.pk).update(started_at=timezone.make_aware(datetime(2026,10,31,22)),
            ended_at=timezone.make_aware(datetime(2026,11,1,8)), daily_minutes={})
        report = self.report(start=date(2026,10,1))
        self.assertEqual(len(report['rows']),2)
        self.assertTrue(all(r['extra'] is None for r in report['rows']))
        self.assertEqual(len(report['reviews']),2)
        self.assertEqual(report['records'][0][0], '미배분')

    def test_plan_states_prior_and_retrospective_registration(self):
        plan = services.save_plan(self.employee, self.data(2), submit=True)
        OvertimePlan.objects.filter(pk=plan.pk).update(submitted_at=timezone.make_aware(datetime(2026,11,1,12)), is_retrospective=False)
        self.record(60, plan=plan)
        self.record(60, day=date(2026,11,3), plan=plan)
        row = self.report()['rows'][0]
        self.assertEqual(row['fixed'], 120)
        self.assertEqual(row['difference'], 0)
        OvertimePlan.objects.filter(pk=plan.pk).update(status='pending')
        row = self.report()['rows'][0]
        self.assertEqual(row['fixed'],0)
        self.assertEqual(row['pending'],120)
        OvertimePlan.objects.filter(pk=plan.pk).update(status='cancelled')
        self.assertEqual(self.report()['rows'][0]['planned'],0)
        OvertimePlan.objects.filter(pk=plan.pk).update(status='approved', is_retrospective=True,
            submitted_at=timezone.make_aware(datetime(2026,11,3,12)))
        self.assertEqual(self.report()['rows'][0]['retrospective'],120)

    def test_xlsx_strings_cannot_be_formulas(self):
        self.record(60)
        self.employee.first_name = '=HYPERLINK("https://evil.example")'
        self.employee.save()
        workbook = load_workbook(BytesIO(export_xlsx(self.report())))
        name = workbook.worksheets[0]['D2']
        self.assertEqual(name.data_type,'s')
        self.assertTrue(name.value.startswith('='))

    def test_permissions_filters_download_and_cache(self):
        self.record(60)
        self.record(600, user=self.outsider)
        self.client.force_login(self.employee)
        base = {'start_month':'2026-11','end_month':'2026-11'}
        path = reverse('monthly_report')
        response = self.client.get(path, base)
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.context['report']['rows']),1)
        invalid = self.client.get(path, dict(base, member=self.outsider.pk, download='xlsx'))
        self.assertContains(invalid,'올바르게 선택')
        self.assertEqual(ReportDownload.objects.count(),0)
        response = self.client.get(path, dict(base, download='xlsx'))
        self.assertEqual(response.status_code,200)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertEqual(len(load_workbook(BytesIO(response.content)).sheetnames),4)
        self.assertEqual(ReportDownload.objects.get().row_count,1)

    def test_allowance_is_separate_and_applies_by_month(self):
        self.record(900)
        ReportAllowance.objects.create(effective_month=date(2026,11,1), included_minutes=600)
        self.policy.threshold_minutes = 30
        self.policy.save()
        self.assertEqual(self.report()['rows'][0]['extra'],300)
        allowance = ReportAllowance(effective_month=date(2026,11,2))
        with self.assertRaises(ValidationError):
            allowance.full_clean()

    def test_limits_empty_and_invalid_months(self):
        self.assertEqual(self.report()['rows'],[])
        with self.assertRaises(ValidationError):
            self.report(start=date(2025,1,1))
        self.record(60)
        with patch('core.reporting.MAX_RECORDS',0):
            with self.assertRaises(ValidationError):
                self.report()


    def test_default_pagination_and_control_characters(self):
        record = self.record(60)
        WorkRecord.objects.filter(pk=record.pk).update(description='개발\x0b업무')
        workbook = load_workbook(BytesIO(export_xlsx(self.report())))
        self.assertEqual(workbook['실적 상세']['P2'].value, '개발업무')
        self.client.force_login(self.admin)
        response = self.client.get(reverse('monthly_report'), {'page': 2})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].is_valid())
        self.assertIn('start_month=', response.context['filter_query'])

    def test_single_month_legacy_inactive_user_and_missing_policy(self):
        record = self.record(60)
        WorkRecord.objects.filter(pk=record.pk).update(daily_minutes={})
        self.employee.is_active = False
        self.employee.save()
        report = self.report()
        self.assertEqual(report['rows'][0]['overtime'], 60)
        self.assertIn('과거 실적', report['rows'][0]['warnings'])
        ReportAllowance.objects.all().delete()
        self.assertIsNone(self.report()['rows'][0]['extra'])

    def test_cross_year_plan_minutes_and_excluded_states(self):
        plan = services.save_plan(self.employee, dict(work_date=date(2026,12,31), end_date=date(2027,1,1),
            start_time=time(23), end_time=time(2), break_minutes=30, title='연말 작업', reason='운영'), submit=True)
        OvertimePlan.objects.filter(pk=plan.pk).update(submitted_at=timezone.make_aware(datetime(2026,12,30,12)), is_retrospective=False)
        report = self.report(start=date(2026,12,1), end=date(2027,1,1))
        self.assertEqual([row['fixed'] for row in report['rows']], [60,90])
        OvertimePlan.objects.filter(pk=plan.pk).update(status='rejected')
        self.assertTrue(all(row['planned'] == 0 for row in self.report(start=date(2026,12,1), end=date(2027,1,1))['rows']))

    def test_malformed_snapshots_and_invalid_filters_fail_safely(self):
        record = self.record(60)
        WorkRecord.objects.filter(pk=record.pk).update(daily_minutes={'bad': {'worked': 'x'}})
        self.assertIsNone(self.report()['rows'][0]['extra'])
        self.client.force_login(self.employee)
        for params in ({'start_month':'invalid', 'end_month':'2026-11'},
                       {'start_month':'9999-12', 'end_month':'9999-12'},
                       {'start_month':'2026-12', 'end_month':'2026-01'}):
            response = self.client.get(reverse('monthly_report'), dict(params, download='xlsx'))
            self.assertEqual(response.status_code,200)
            self.assertNotIn('Content-Disposition', response)
        self.assertEqual(ReportDownload.objects.count(),0)

    def test_linked_plan_excess_flag(self):
        plan = services.save_plan(self.employee, self.data(2), submit=True)
        self.record(180, plan=plan)
        self.assertIn('계획 초과 수행', self.report()['rows'][0]['warnings'])
