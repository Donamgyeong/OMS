"""Transactional organization edits shared by setup and Django admin."""
import hashlib
import json
from django.contrib.admin.models import CHANGE, ADDITION, LogEntry
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from .models import ApprovalSettings, Department, User

MEMBER_FIELDS = ('first_name', 'last_name', 'email', 'employee_number', 'department', 'role',
                 'is_active', 'designated_approver', 'daily_work_minutes', 'working_weekdays', 'onboarding_completed')
DEPARTMENT_FIELDS = ('name', 'parent', 'head', 'active')


def revision(obj):
    fields = MEMBER_FIELDS + ('is_superuser', 'is_staff') if isinstance(obj, User) else DEPARTMENT_FIELDS
    values = [getattr(obj, obj._meta.get_field(name).attname) for name in fields]
    return hashlib.sha256(json.dumps(values, default=str, ensure_ascii=False).encode()).hexdigest()


def _check_revision(obj, expected):
    if expected and revision(obj) != expected:
        raise ValidationError('다른 관리자가 정보를 변경했습니다. 새로고침 후 변경 내용을 확인하세요.')


def validate_member(member):
    if member.role == User.Role.MANAGER and member.is_active:
        if not member.department_id or not member.department.active:
            raise ValidationError('활성 부서장에게는 활성 소속 부서가 필요합니다.')
        if Department.objects.filter(pk=member.department_id).exclude(head_id__in=[member.pk]).exclude(head=None).exists():
            raise ValidationError('해당 부서에 다른 부서장이 있습니다. 부서 정보에서 부서장을 명시적으로 교체하세요.')
        if User.objects.filter(department_id=member.department_id, is_active=True, role=User.Role.MANAGER).exclude(pk=member.pk).exists():
            raise ValidationError('해당 부서에 다른 부서장 역할 사용자가 있습니다. 기존 역할과 부서 정보를 먼저 정리하세요.')


def validate_department(department):
    if not department.active and department.pk:
        if User.objects.filter(department=department, is_active=True).exists():
            raise ValidationError('활성 사용자가 있는 부서는 비활성화할 수 없습니다. 먼저 사용자를 이동하거나 비활성화하세요.')
        if Department.objects.filter(parent=department, active=True).exists():
            raise ValidationError('활성 하위 부서가 있습니다. 먼저 상위 부서를 변경하거나 하위 부서를 비활성화하세요.')
    if department.head_id:
        head = department.head
        if not department.active or not head.is_active or head.department_id != department.pk:
            raise ValidationError('부서장은 해당 활성 부서에 소속된 활성 사용자여야 합니다.')
        if head.role == User.Role.ADMIN and not head.is_superuser:
            raise ValidationError('운영 관리자를 부서장으로 지정하려면 먼저 사용자 역할을 변경하세요.')


def _lock():
    # Serialize organization writes, including concurrent edits of different members.
    ApprovalSettings.objects.get_or_create(pk=1)
    ApprovalSettings.objects.select_for_update().get(pk=1)
    list(Department.objects.select_for_update().order_by('pk'))


def _authorize(actor, member=None):
    if not actor.is_active or not (actor.is_superuser or actor.role == User.Role.ADMIN):
        raise PermissionDenied
    if member and member.is_superuser and not actor.is_superuser:
        raise PermissionDenied('최고관리자의 정보는 최고관리자만 변경할 수 있습니다.')


def _audit(actor, obj, before, fields):
    after = {name: getattr(obj, name + '_id' if name in ('department', 'head', 'parent') else name) for name in fields}
    changes = [f'{name}: {before.get(name)} → {value}' for name, value in after.items() if before.get(name) != value]
    if changes:
        LogEntry.objects.log_actions(user_id=actor.pk, queryset=[obj], action_flag=CHANGE if before else ADDITION,
            change_message='조직 정보: ' + '; '.join(changes))


def _snapshot(obj, fields):
    return {name: getattr(obj, name + '_id' if name in ('department', 'head', 'parent') else name) for name in fields} if obj.pk else {}


@transaction.atomic
def save_member(actor, submitted, fields=MEMBER_FIELDS, expected_revision=None):
    _lock()
    member = User.objects.select_for_update().get(pk=submitted.pk)
    _authorize(actor, member)
    allowed = set(MEMBER_FIELDS) | ({'is_staff', 'is_superuser'} if actor.is_superuser else set())
    if set(fields) - allowed:
        raise PermissionDenied('허용되지 않은 사용자 필드를 변경할 수 없습니다.')
    _check_revision(member, expected_revision)
    before = _snapshot(member, fields)
    was_active = member.is_active
    for name in fields:
        setattr(member, name, getattr(submitted, name))
    # Never trust related objects cached during an earlier form validation.
    if member.department_id:
        member.department = Department.objects.get(pk=member.department_id)
    if member.designated_approver_id:
        member.designated_approver = User.objects.get(pk=member.designated_approver_id)
    validate_member(member)
    member.full_clean()
    member.save(update_fields=list(fields))
    keeps_head = member.is_active and (member.role == User.Role.MANAGER or
                                      (member.is_superuser and member.role == User.Role.ADMIN))
    for department in Department.objects.filter(head=member).exclude(
            pk=member.department_id if keeps_head else None):
        old = _snapshot(department, DEPARTMENT_FIELDS)
        department.head = None
        department.save(update_fields=['head'])
        _audit(actor, department, old, DEPARTMENT_FIELDS)
    if member.is_active and member.role == User.Role.MANAGER:
        department = Department.objects.get(pk=member.department_id)
        old = _snapshot(department, DEPARTMENT_FIELDS)
        department.head = member
        department.full_clean()
        department.save(update_fields=['head'])
        _audit(actor, department, old, DEPARTMENT_FIELDS)
    # Revalidate against the final organization state, not the old department head cache.
    if member.department_id:
        member.department = Department.objects.get(pk=member.department_id)
    member.full_clean()
    if was_active != member.is_active:
        from .authentication import revoke_sessions
        revoke_sessions(member.pk)
    _audit(actor, member, before, fields)
    return member


@transaction.atomic
def save_department(actor, submitted, expected_revision=None):
    _lock()
    _authorize(actor)
    department = Department.objects.get(pk=submitted.pk) if submitted.pk else Department()
    _check_revision(department, expected_revision)
    before = _snapshot(department, DEPARTMENT_FIELDS)
    previous_head_id = department.head_id
    for name in DEPARTMENT_FIELDS:
        setattr(department, name, getattr(submitted, name))
    ids = {value for value in (previous_head_id, department.head_id) if value}
    members = {m.pk: m for m in User.objects.select_for_update().filter(pk__in=ids).order_by('pk')}
    if department.head_id:
        department.head = members[department.head_id]
    if department.parent_id:
        department.parent = Department.objects.get(pk=department.parent_id)
    validate_department(department)
    department.full_clean()
    department.save()
    # Demote the outgoing head before validating the incoming manager, regardless of IDs.
    ordered = sorted(members.values(), key=lambda member: member.pk == department.head_id)
    for member in ordered:
        new_role = member.role
        if member.pk == department.head_id and not (member.is_superuser and member.role == User.Role.ADMIN):
            new_role = User.Role.MANAGER
        elif member.pk == previous_head_id and member.department_id == department.pk and member.role == User.Role.MANAGER:
            new_role = User.Role.EMPLOYEE
        if new_role != member.role:
            _authorize(actor, member)
            old = _snapshot(member, MEMBER_FIELDS)
            member.role = new_role
            if member.department_id:
                member.department = Department.objects.get(pk=member.department_id)
            member.full_clean()
            member.save(update_fields=['role'])
            _audit(actor, member, old, MEMBER_FIELDS)
    if User.objects.filter(department=department, is_active=True, role=User.Role.MANAGER).exclude(pk=department.head_id).exists():
        raise ValidationError('부서장 지정과 다른 부서장 역할 사용자가 있습니다. 사용자 역할을 먼저 정리하세요.')
    _audit(actor, department, before, DEPARTMENT_FIELDS)
    return department
