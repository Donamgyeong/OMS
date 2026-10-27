"""All employee writes use these transactions; user rows serialize monthly decisions."""
from datetime import datetime, time, timedelta
import json
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone
from .models import (ApprovalEvent, Department, Holiday, Notification, OvertimePlan,
                     OvertimePolicy, User, WorkRecord, WorkRecordEvent)

COUNTED = (OvertimePlan.Status.CONFIRMED, OvertimePlan.Status.PENDING, OvertimePlan.Status.APPROVED)
PLAN_FIELDS = ("work_date", "end_date", "start_time", "end_time", "break_minutes", "title", "reason")
RECORD_FIELDS = ("work_date", "started_at", "ended_at", "break_minutes", "plan", "description", "exception_reason")


def snapshot(obj):
    # Include computed noneditable minutes and preserve structured monthly policy evidence.
    values = {field.name: getattr(obj, field.attname) for field in obj._meta.concrete_fields}
    return json.loads(json.dumps(values, cls=DjangoJSONEncoder))


def daily_slices(start, end, break_minutes):
    """Break is deducted from the end, so cross-month attribution is deterministic."""
    end -= timedelta(minutes=break_minutes)
    result = {}
    while start < end:
        boundary = datetime.combine(start.date() + timedelta(days=1), time.min)
        stop = min(end, boundary)
        result[start.date()] = int((stop - start).total_seconds() // 60)
        start = stop
    return result


def plan_minutes(plan):
    return daily_slices(datetime.combine(plan.work_date, plan.start_time),
                        datetime.combine(plan.end_date or plan.work_date, plan.end_time), plan.break_minutes)


def monthly_minutes(plan):
    result = {}
    for day, minutes in plan_minutes(plan).items():
        month = day.strftime("%Y-%m")
        result[month] = result.get(month, 0) + minutes
    return result


def policy_on(day):
    policy = OvertimePolicy.objects.filter(effective_from__lte=day).first()
    if policy is None:
        raise ValidationError(f"{day}에 적용할 초과 근무 정책이 없습니다. 관리자에게 정책 등록을 요청하세요.")
    return policy


def default_approver(user):
    if user.is_superuser:
        candidate = user
    elif user.role == User.Role.MANAGER or Department.objects.filter(head=user).exists():
        from .models import ApprovalSettings
        config = ApprovalSettings.objects.filter(pk=1).first()
        if config and config.default_administrator_id:
            candidate = config.default_administrator
            if not candidate.is_superuser:
                raise ValidationError("기본 최고관리자의 권한을 확인하세요.")
        else:
            candidates = list(User.objects.filter(is_superuser=True, is_active=True)[:2])
            if len(candidates) != 1:
                raise ValidationError("기본 최고관리자를 설정하세요.")
            candidate = candidates[0]
    else:
        candidate = user.department.head if user.department_id else None
    return validate_approver(user, candidate)


def validate_approver(user, candidate):
    if (not candidate or not candidate.is_active
            or (candidate.pk == user.pk and not user.is_superuser)
            or (candidate.department_id and not candidate.department.active)):
        raise ValidationError("유효한 승인자가 없습니다. 부서장 또는 지정 승인자를 확인하세요.")
    return candidate


def resolve_approver(user):
    if user.designated_approver_id:
        return validate_approver(user, user.designated_approver)
    return default_approver(user)


def can_decide(user, plan):
    return (user.is_active and plan.approver_id == user.pk
            and (plan.user_id != user.pk or user.is_superuser)
            and plan.status == OvertimePlan.Status.PENDING)


def check_version(obj, version):
    if version != obj.version:
        raise ValidationError("다른 창에서 변경되거나 이미 처리되었습니다. 새로고침 후 다시 시도하세요.")


def event(plan, actor, comment="", before=None):
    entry = ApprovalEvent.objects.create(plan=plan, actor=actor, result=plan.status,
        comment=comment, version=plan.version, snapshot={"before": before, "after": snapshot(plan)})
    from .teams import enqueue
    enqueue(entry, plan, actor)


@transaction.atomic
def save_plan(actor, data, *, pk=None, version=None, submit=False):
    owner = User.objects.select_for_update().get(pk=actor.pk)
    if not owner.department_id or not owner.department.active:
        raise ValidationError("활성 부서에 소속되어야 계획을 등록할 수 있습니다.")
    plan = OvertimePlan.objects.select_for_update().get(pk=pk, user=actor) if pk else OvertimePlan(user=owner, department=owner.department)
    before = snapshot(plan) if pk else None
    if pk:
        check_version(plan, version)
        if plan.status not in (OvertimePlan.Status.DRAFT, OvertimePlan.Status.REJECTED, OvertimePlan.Status.CONFIRMED, OvertimePlan.Status.APPROVED):
            raise ValidationError("승인 대기 계획은 회수 후 수정하세요. 취소한 계획은 변경할 수 없습니다.")
        plan.version += 1
    for field in PLAN_FIELDS:
        setattr(plan, field, data[field])
    plan.full_clean()
    plan.status = OvertimePlan.Status.DRAFT
    plan.approver = None
    plan.submitted_at = None
    plan.submitted_total_minutes = None
    plan.threshold_snapshot = None
    plan.monthly_snapshot = {}
    plan.policy_snapshot = {}
    if submit:
        decide_submission(plan, owner)
    plan.save()
    event(plan, actor, "제출" if submit else "임시 저장", before)
    if plan.status == OvertimePlan.Status.PENDING:
        Notification.objects.create(user=plan.approver, plan=plan, message=f"{actor.display_name}: {plan.title} 결재 요청")
    return plan


def decide_submission(plan, owner):
    allocations = monthly_minutes(plan)
    totals = {month: 0 for month in allocations}
    for other in OvertimePlan.objects.filter(user=owner, status__in=COUNTED).exclude(pk=plan.pk):
        for month, minutes in monthly_minutes(other).items():
            if month in totals:
                totals[month] += minutes
    needs_approval = False
    policies = {}
    for month, minutes in allocations.items():
        # The policy effective on the first work date in each month applies.
        day = max(plan.work_date, datetime.strptime(month, "%Y-%m").date())
        policy = policy_on(day)
        total = totals[month] + minutes
        needs_approval |= total > policy.threshold_minutes
        plan.monthly_snapshot[month] = {"previous": totals[month], "requested": minutes,
            "total": total, "threshold": policy.threshold_minutes, "remaining": max(0, policy.threshold_minutes - total),
            "requires_approval": total > policy.threshold_minutes}
        policies[month] = {"id": policy.pk, "effective_from": str(policy.effective_from), "threshold": policy.threshold_minutes}
    first = next(iter(plan.monthly_snapshot.values()))
    plan.submitted_total_minutes = first["total"]
    plan.threshold_snapshot = first["threshold"]
    plan.policy_snapshot = policies
    plan.status = OvertimePlan.Status.PENDING if needs_approval else OvertimePlan.Status.CONFIRMED
    if needs_approval:
        plan.approver = resolve_approver(owner)
        plan.policy_snapshot["approval_route"] = {
            "mode": "designated" if owner.designated_approver_id else "automatic",
            "role": owner.role, "is_superuser": owner.is_superuser,
            "approver_id": plan.approver_id}
    plan.submitted_at = timezone.now()
    start = timezone.make_aware(datetime.combine(plan.work_date, plan.start_time))
    plan.is_retrospective = start <= plan.submitted_at


@transaction.atomic
def transition_plan(actor, pk, action, version, comment=""):
    owner_id = OvertimePlan.objects.values_list("user_id", flat=True).get(pk=pk)
    User.objects.select_for_update().get(pk=owner_id)
    plan = OvertimePlan.objects.select_for_update().get(pk=pk)
    check_version(plan, version)
    before = snapshot(plan)
    if action in ("approve", "reject"):
        if not actor.is_active or plan.approver_id != actor.pk or (plan.user_id == actor.pk and not actor.is_superuser):
            raise PermissionDenied
        if plan.status != OvertimePlan.Status.PENDING:
            raise ValidationError("승인 대기 상태에서만 결재할 수 있습니다.")
        if action == "reject" and not comment.strip():
            raise ValidationError("반려 사유를 입력하세요.")
        plan.status = OvertimePlan.Status.APPROVED if action == "approve" else OvertimePlan.Status.REJECTED
        target = plan.user
    elif action in ("recall", "cancel"):
        if plan.user_id != actor.pk:
            raise PermissionDenied
        if action == "recall" and plan.status != OvertimePlan.Status.PENDING:
            raise ValidationError("승인 대기 중에만 회수할 수 있습니다.")
        if plan.status == OvertimePlan.Status.CANCELLED:
            raise ValidationError("이미 취소되었습니다.")
        plan.status = OvertimePlan.Status.DRAFT if action == "recall" else OvertimePlan.Status.CANCELLED
        target = plan.approver
    else:
        raise ValidationError("지원하지 않는 처리입니다.")
    plan.version += 1
    plan.save()
    event(plan, actor, comment.strip(), before)
    if target:
        Notification.objects.create(user=target, plan=plan, message=f"{plan.title}: {plan.get_status_display()}")
    return plan


@transaction.atomic
def save_record(actor, data, *, pk=None, version=None, change_reason=""):
    owner = User.objects.select_for_update().get(pk=actor.pk)
    if not owner.department_id or not owner.department.active:
        raise ValidationError("활성 부서에 소속되어야 근무를 등록할 수 있습니다.")
    record = WorkRecord.objects.select_for_update().get(pk=pk, user=actor) if pk else WorkRecord(user=owner, department=owner.department)
    before = snapshot(record) if pk else None
    if pk:
        check_version(record, version)
        if not change_reason.strip():
            raise ValidationError("수정 사유를 입력하세요.")
        record.version += 1
    for field in RECORD_FIELDS:
        setattr(record, field, data[field])
    if record.plan_id:
        # The form's plan object may predate a concurrent decision; reread under the owner lock.
        record.plan = OvertimePlan.objects.get(pk=record.plan_id)
    record.overtime_minutes = 0
    record.full_clean()
    start = timezone.localtime(record.started_at).replace(tzinfo=None)
    end = timezone.localtime(record.ended_at).replace(tzinfo=None)
    days = daily_slices(start, end, record.break_minutes)
    weekdays = {int(value) for value in owner.working_weekdays.split(",")}
    holidays = set(Holiday.objects.filter(date__in=days).values_list("date", flat=True))
    # One complete attendance interval per calendar day avoids duplicate daily allowances.
    for other in WorkRecord.objects.filter(user=owner).exclude(pk=record.pk):
        other_days = daily_slices(timezone.localtime(other.started_at).replace(tzinfo=None),
            timezone.localtime(other.ended_at).replace(tzinfo=None), other.break_minutes)
        if set(other_days) & set(days):
            raise ValidationError("같은 날짜의 근무 기록이 있습니다. 기존 기록에 하루 전체 근무를 입력하세요.")
    record.daily_minutes = {}
    for day, minutes in days.items():
        standard = owner.daily_work_minutes if day.weekday() in weekdays and day not in holidays else 0
        record.daily_minutes[str(day)] = {"worked": minutes, "overtime": max(0, minutes - standard), "standard": standard}
    record.overtime_minutes = sum(value["overtime"] for value in record.daily_minutes.values())
    record.duration_minutes = sum(days.values())
    if record.plan_id:
        if record.plan.user_id != actor.pk:
            raise ValidationError("본인 계획만 연결할 수 있습니다.")
        planned_days = set(plan_minutes(record.plan))
        if not set(days).intersection(planned_days):
            raise ValidationError("근무일과 연결 계획의 날짜가 겹쳐야 합니다.")
        linked = sum(WorkRecord.objects.filter(plan=record.plan).exclude(pk=record.pk).values_list("overtime_minutes", flat=True))
        excess = linked + record.overtime_minutes > record.plan.duration_minutes
    else:
        excess = record.overtime_minutes > 0
    if excess and not record.exception_reason.strip():
        raise ValidationError("미계획 또는 계획 초과 근무 사유를 입력하세요.")
    record.unapproved_at_entry = False
    if record.plan_id:
        plan = record.plan
        approval = plan.approval_events.filter(result=OvertimePlan.Status.APPROVED, version=plan.version).first()
        effective_at = approval.created_at if plan.status == OvertimePlan.Status.APPROVED and approval else plan.submitted_at
        record.unapproved_at_entry = (plan.status not in (OvertimePlan.Status.CONFIRMED, OvertimePlan.Status.APPROVED)
            or effective_at is None or effective_at > record.started_at)
    record.save()
    WorkRecordEvent.objects.create(record=record, actor=actor, reason=change_reason.strip() or "최초 등록",
        snapshot={"before": before, "after": snapshot(record)})
    return record
