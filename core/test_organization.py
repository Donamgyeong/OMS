from io import StringIO
from django.contrib.admin.models import LogEntry
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from .forms import MemberForm, DepartmentForm
from .models import User, Department
from .organization import save_member, save_department, revision
from .test_workflow import WorkflowFixture


class OrganizationTests(WorkflowFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.root = User.objects.create_user('root', is_superuser=True, is_staff=True, role='admin', onboarding_completed=True)
        # Complete business metadata required for the production edit validations.
        for user in (self.employee, self.manager, self.outsider):
            user.employee_number = f'E{user.pk}'
            user.onboarding_completed = False
            user.save()

    def member_data(self, member, **changes):
        data = dict(first_name=member.first_name, last_name=member.last_name, email=member.email,
            employee_number=member.employee_number or '', department=member.department_id or '',
            role=member.role, is_active='on' if member.is_active else '',
            designated_approver=member.designated_approver_id or '', daily_work_minutes=480,
            working_weekdays='0,1,2,3,4', onboarding_completed='on' if member.onboarding_completed else '')
        data.update(changes)
        return data

    def test_promoting_member_updates_department_and_logs_both(self):
        self.other_department.head = None
        self.other_department.save()
        form = MemberForm(self.member_data(self.outsider, role='manager'), instance=self.outsider)
        self.assertTrue(form.is_valid(), form.errors)
        form.save_organization(self.root)
        self.other_department.refresh_from_db()
        self.outsider.refresh_from_db()
        self.assertEqual(self.other_department.head, self.outsider)
        self.assertEqual(self.outsider.role, 'manager')
        self.assertEqual(LogEntry.objects.filter(user=self.root).count(), 2)

    def test_conflicting_promotion_is_rejected(self):
        form = MemberForm(self.member_data(self.employee, role='manager'), instance=self.employee)
        self.assertFalse(form.is_valid())
        self.assertIn('다른 부서장', str(form.errors))
        self.department.refresh_from_db()
        self.assertEqual(self.department.head, self.manager)

    def test_department_replacement_synchronizes_both_roles(self):
        form = DepartmentForm(dict(name='개발', parent='', head=self.employee.pk, active='on'), instance=self.department)
        self.assertTrue(form.is_valid(), form.errors)
        form.save_organization(self.root)
        self.manager.refresh_from_db()
        self.employee.refresh_from_db()
        self.department.refresh_from_db()
        self.assertEqual(self.manager.role, 'employee')
        self.assertEqual(self.employee.role, 'manager')
        self.assertEqual(self.department.head, self.employee)

    def test_manager_transfer_removes_old_department_head(self):
        self.manager.department = self.other_department
        save_member(self.root, self.manager)
        self.department.refresh_from_db()
        self.other_department.refresh_from_db()
        self.assertIsNone(self.department.head)
        self.assertEqual(self.other_department.head, self.manager)

    def test_deactivation_clears_head_and_revokes_sessions(self):
        self.manager.is_active = False
        old_version = self.manager.auth_version
        save_member(self.root, self.manager)
        self.department.refresh_from_db()
        self.manager.refresh_from_db()
        self.assertIsNone(self.department.head)
        self.assertGreater(self.manager.auth_version, old_version)

    def test_department_clear_demotes_previous_head(self):
        self.department.head = None
        save_department(self.root, self.department)
        self.manager.refresh_from_db()
        self.assertEqual(self.manager.role, 'employee')

    def test_demoting_manager_clears_department_head(self):
        self.manager.role = 'employee'
        save_member(self.root, self.manager)
        self.department.refresh_from_db()
        self.assertIsNone(self.department.head)

    def test_invalid_department_changes(self):
        for values in (dict(head=self.outsider.pk, active='on'), dict(head='', active='')):
            form = DepartmentForm(dict(name='개발', parent='', **values), instance=Department.objects.get(pk=self.department.pk))
            self.assertFalse(form.is_valid())
        self.other_department.parent = self.department
        self.other_department.save()
        self.department.parent = self.other_department
        with self.assertRaises(ValidationError):
            self.department.full_clean()

    def test_invalid_work_and_identity_fields(self):
        for changes in (dict(role='manager', department=''), dict(daily_work_minutes=0),
                        dict(daily_work_minutes=1441), dict(working_weekdays='0,0,1'),
                        dict(designated_approver=self.employee.pk), dict(employee_number=self.manager.employee_number)):
            with self.subTest(changes=changes):
                form = MemberForm(self.member_data(self.employee, **changes), instance=User.objects.get(pk=self.employee.pk))
                self.assertFalse(form.is_valid())
        form = MemberForm(self.member_data(self.employee, employee_number='  ', working_weekdays='0, 1,2'), instance=self.employee)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save_organization(self.root)
        self.assertIsNone(saved.employee_number)
        self.assertEqual(saved.working_weekdays, '0,1,2')

    def test_stale_submission_is_rejected_under_lock(self):
        expected = revision(self.outsider)
        User.objects.filter(pk=self.outsider.pk).update(first_name='다른 관리자 수정')
        self.outsider.role = 'manager'
        with self.assertRaises(ValidationError):
            save_member(self.root, self.outsider, expected_revision=expected)
        self.other_department.refresh_from_db()
        self.assertIsNone(self.other_department.head)

    def test_non_superuser_cannot_indirectly_change_superuser_role(self):
        self.root.department = self.department
        self.root.role = 'employee'
        self.root.save()
        self.department.head = self.root
        with self.assertRaises(PermissionDenied):
            save_department(self.admin, self.department)
        self.department.refresh_from_db()
        self.assertEqual(self.department.head, self.manager)

    def test_setup_and_admin_use_the_same_save_rules(self):
        self.client.force_login(self.root)
        self.assertEqual(self.client.post(reverse('setup_edit', args=['member', self.outsider.pk]),
            self.member_data(self.outsider, role='manager')).status_code, 302)
        self.other_department.refresh_from_db()
        self.assertEqual(self.other_department.head, self.outsider)
        response = self.client.post(f'/admin/core/department/{self.department.pk}/change/',
            dict(name='개발', parent='', head=self.employee.pk, active='on', _save='저장'))
        self.assertEqual(response.status_code, 302)
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.role, 'manager')

    def test_admin_user_promotion_and_revision_field(self):
        self.client.force_login(self.root)
        path = f'/admin/core/user/{self.outsider.pk}/change/'
        self.assertContains(self.client.get(path), 'name="organization_revision"')
        response = self.client.post(path, self.member_data(self.outsider, role='manager', _save='저장'))
        self.assertEqual(response.status_code, 302)
        self.other_department.refresh_from_db()
        self.assertEqual(self.other_department.head, self.outsider)

    def test_organization_save_rechecks_conflicts_after_form_validation(self):
        form = MemberForm(self.member_data(self.outsider, role='manager'), instance=self.outsider)
        self.assertTrue(form.is_valid(), form.errors)
        replacement = User.objects.create_user('replacement', department=self.other_department)
        Department.objects.filter(pk=self.other_department.pk).update(head=replacement)
        with self.assertRaises(ValidationError):
            form.save_organization(self.root)
        self.outsider.refresh_from_db()
        self.assertEqual(self.outsider.role, 'employee')

    def test_completed_member_invalid_promotion_rolls_back_everything(self):
        self.root.is_active = False
        self.root.save()
        self.outsider.onboarding_completed = True
        self.outsider.designated_approver = self.employee
        self.outsider.save()
        # Remove the designated route; no active highest administrator remains.
        self.outsider.designated_approver = None
        self.outsider.role = 'manager'
        with self.assertRaises(ValidationError):
            save_member(self.admin, self.outsider)
        self.other_department.refresh_from_db()
        self.outsider.refresh_from_db()
        self.assertIsNone(self.other_department.head)
        self.assertEqual(self.outsider.role, 'employee')

    def test_blank_employee_numbers_do_not_conflict_and_invalid_fields_do_not_crash(self):
        for member in (self.employee, self.outsider):
            form = MemberForm(self.member_data(member, employee_number=''), instance=member)
            self.assertTrue(form.is_valid(), form.errors)
            form.save_organization(self.root)
        for value in ('', 'abc', '-1'):
            form = MemberForm(self.member_data(self.employee, daily_work_minutes=value), instance=self.employee)
            self.assertFalse(form.is_valid())

    def test_audit_repairs_unambiguous_old_data_and_reports_conflicts(self):
        from django.core.management import call_command
        User.objects.filter(pk=self.outsider.pk).update(role='manager')
        output = StringIO()
        call_command('audit_organization', stdout=output)
        self.assertIn('불일치 부서 1개', output.getvalue())
        self.other_department.refresh_from_db()
        self.assertIsNone(self.other_department.head)
        call_command('audit_organization', repair=True, actor=self.root.pk, stdout=StringIO())
        self.other_department.refresh_from_db()
        self.assertEqual(self.other_department.head, self.outsider)
        User.objects.create_user('conflict', department=self.other_department, role='manager')
        output = StringIO()
        call_command('audit_organization', repair=True, actor=self.root.pk, stdout=output)
        self.assertIn('수동 확인 필요', output.getvalue())

    def test_saved_plan_approver_remains_unchanged_after_department_replacement(self):
        plan = self.submit(17)
        self.department.head = self.employee
        save_department(self.root, self.department)
        plan.refresh_from_db()
        self.assertEqual(plan.approver, self.manager)

    def test_service_rejects_admin_privilege_escalation(self):
        self.employee.is_superuser = True
        with self.assertRaises(PermissionDenied):
            save_member(self.admin, self.employee, fields=['is_superuser'])
        self.employee.refresh_from_db()
        self.assertFalse(self.employee.is_superuser)

    def test_inactive_users_keep_historical_department_when_editing(self):
        User.objects.filter(pk=self.outsider.pk).update(is_active=False)
        Department.objects.filter(pk=self.other_department.pk).update(active=False)
        self.outsider.refresh_from_db()
        form = MemberForm(self.member_data(self.outsider, first_name='퇴사자 정정'), instance=self.outsider)
        self.assertTrue(form.is_valid(), form.errors)
        form.save_organization(self.root)
        self.outsider.refresh_from_db()
        self.assertEqual(self.outsider.department_id, self.other_department.pk)

    def test_stale_department_form_does_not_overwrite_new_head(self):
        expected = revision(self.department)
        self.department.name = '먼저 변경'
        save_department(self.root, self.department)
        submitted = Department.objects.get(pk=self.department.pk)
        submitted.head = self.employee
        with self.assertRaises(ValidationError):
            save_department(self.root, submitted, expected_revision=expected)
        self.department.refresh_from_db()
        self.assertEqual(self.department.head, self.manager)

    def test_direct_session_deactivation_clears_head(self):
        from .authentication import revoke_sessions
        revoke_sessions(self.manager.pk, deactivate=True)
        self.department.refresh_from_db()
        self.manager.refresh_from_db()
        self.assertFalse(self.manager.is_active)
        self.assertIsNone(self.department.head)

    def test_admin_bootstrap_clears_outgoing_manager_link(self):
        from uuid import UUID
        from django.test import override_settings
        from django.core.management import call_command
        from .models import EntraIdentity
        tenant = '11111111-1111-1111-1111-111111111111'
        object_id = '22222222-2222-2222-2222-222222222222'
        EntraIdentity.objects.create(user=self.manager, tenant_id=UUID(tenant), object_id=UUID(object_id))
        with override_settings(ENTRA_CONFIGURED=True, ENTRA_TENANT_ID=tenant):
            call_command('bootstrap_entra_admin', object_id, stdout=StringIO())
        self.department.refresh_from_db()
        self.manager.refresh_from_db()
        self.assertEqual(self.manager.role, 'admin')
        self.assertIsNone(self.department.head)

    def test_setup_department_save_synchronizes_completed_members_with_revision(self):
        User.objects.filter(pk__in=[self.employee.pk, self.manager.pk]).update(onboarding_completed=True)
        self.client.force_login(self.admin)
        path = reverse('setup_edit', args=['department', self.department.pk])
        form = self.client.get(path).context['form']
        response = self.client.post(path, dict(name=self.department.name, parent='',
            head=self.employee.pk, active='on', organization_revision=form['organization_revision'].value()), follow=True)
        self.assertEqual(response.redirect_chain, [(reverse('setup'), 302)])
        self.assertContains(response, '정보를 저장했습니다')
        self.department.refresh_from_db()
        self.employee.refresh_from_db()
        self.manager.refresh_from_db()
        self.assertEqual(self.department.head_id, self.employee.pk)
        self.assertEqual(self.employee.role, User.Role.MANAGER)
        self.assertEqual(self.manager.role, User.Role.EMPLOYEE)

    def test_setup_member_save_synchronizes_completed_member_with_revision(self):
        # An actual completed employee has an approver before promotion.
        self.outsider.onboarding_completed = True
        self.outsider.designated_approver = self.manager
        self.outsider.save()
        self.client.force_login(self.admin)
        path = reverse('setup_edit', args=['member', self.outsider.pk])
        form = self.client.get(path).context['form']
        response = self.client.post(path, self.member_data(self.outsider, role='manager',
            organization_revision=form['organization_revision'].value()), follow=True)
        self.assertEqual(response.redirect_chain, [(reverse('setup'), 302)])
        self.assertContains(response, '정보를 저장했습니다')
        self.other_department.refresh_from_db()
        self.outsider.refresh_from_db()
        self.assertEqual(self.other_department.head_id, self.outsider.pk)
        self.assertEqual(self.outsider.role, User.Role.MANAGER)
