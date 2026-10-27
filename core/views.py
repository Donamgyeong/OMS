from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.models import Q
from django.http import JsonResponse, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST
from datetime import datetime
from .models import ApprovalSettings, HolidaySyncState, Department, Holiday, OvertimePlan, OvertimePolicy, User, WorkRecord, Notification
from .selectors import visible_to
from .forms import ApprovalSettingsForm, PlanForm, RecordForm, DecisionForm, DepartmentForm, HolidayForm, MemberForm, PolicyForm
from . import services


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


@login_required
def dashboard(request):
    today = timezone.localdate()
    month = request.GET.get("month", today.strftime("%Y-%m"))
    try:
        selected = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        raise Http404("월 형식을 확인하세요.")
    plans = visible_to(OvertimePlan.objects.select_related("user", "department").prefetch_related("workrecord_set"), request.user)
    records = visible_to(WorkRecord.objects.select_related("user", "department", "plan"), request.user)
    own_total = sum(services.monthly_minutes(p).get(month, 0) for p in OvertimePlan.objects.filter(user=request.user, status__in=services.COUNTED))
    if request.GET.get("status"):
        plans = plans.filter(status=request.GET["status"])
    for field in ("department_id", "user_id"):
        value = request.GET.get(field)
        if value:
            if not value.isdecimal():
                raise Http404
            plans, records = plans.filter(**{field: value}), records.filter(**{field: value})
    plans = [plan for plan in plans if month in services.monthly_minutes(plan)]
    actual_filter = request.GET.get("actual")
    if actual_filter in ("registered", "missing"):
        plans = [plan for plan in plans if bool(plan.workrecord_set.all()) == (actual_filter == "registered")]
    records = [record for record in records if any(day.startswith(month) for day in record.daily_minutes) or (not record.daily_minutes and record.work_date.strftime("%Y-%m") == month)]
    for plan in plans:
        plan.month_minutes = services.monthly_minutes(plan)[month]
        plan.actual = sum(record.overtime_minutes for record in plan.workrecord_set.all())
        plan.difference = plan.actual - plan.duration_minutes
    for record in records:
        record.month_overtime = sum(value["overtime"] for day, value in record.daily_minutes.items() if day.startswith(month)) if record.daily_minutes else record.overtime_minutes
        record.month_worked = sum(value["worked"] for day, value in record.daily_minutes.items() if day.startswith(month)) if record.daily_minutes else record.duration_minutes
    totals = {status: sum(p.month_minutes for p in plans if p.status == status) for status in services.COUNTED}
    try:
        threshold = services.policy_on(max(selected, today) if selected.strftime("%Y-%m") == today.strftime("%Y-%m") else selected).threshold_minutes
    except ValidationError:
        threshold = None
    return render(request, "core/dashboard.html", {
        "month": selected, "selected_month": month, "plans": plans, "records": records,
        "planned_minutes": sum(totals.values()), "actual_minutes": sum(r.month_overtime for r in records),
        "worked_minutes": sum(r.month_worked for r in records),
        "pending_count": sum(p.status == OvertimePlan.Status.PENDING for p in plans),
        "confirmed_minutes": totals[OvertimePlan.Status.CONFIRMED], "approved_minutes": totals[OvertimePlan.Status.APPROVED],
        "pending_minutes": totals[OvertimePlan.Status.PENDING], "statuses": OvertimePlan.Status.choices,
        "threshold": threshold, "own_total": own_total, "remaining": max(0, threshold - own_total) if threshold is not None else None,
        "notifications": Notification.objects.filter(user=request.user, read=False).order_by("-created_at")[:20],
        "members": visible_members(request.user),
        "departments": Department.objects.filter(pk__in=visible_to(OvertimePlan.objects.all(), request.user).values("department_id")).union(
            Department.objects.filter(pk__in=visible_members(request.user).values("department_id"))),
    })


def accessible_plan(user, pk):
    scope = visible_to(OvertimePlan.objects.all(), user).values("pk")
    return get_object_or_404(OvertimePlan.objects.select_related("user", "department", "approver").filter(
        Q(pk__in=scope) | Q(approver=user)), pk=pk)


@login_required
def plan_edit(request, pk=None):
    plan = get_object_or_404(OvertimePlan, pk=pk, user=request.user) if pk else OvertimePlan(user=request.user)
    form = PlanForm(request.POST or None, instance=plan)
    preview = None
    if request.method == "POST" and form.is_valid():
        try:
            action = request.POST.get("intent", "save")
            if action not in ("preview", "save", "submit"):
                raise ValidationError("지원하지 않는 처리입니다.")
            if action == "preview":
                plan.monthly_snapshot = {}
                services.decide_submission(plan, request.user)
                preview = plan
            else:
                saved = services.save_plan(request.user, form.cleaned_data, pk=pk,
                    version=form.cleaned_data["version"], submit=action == "submit")
                messages.success(request, f"{saved.get_status_display()} 처리되었습니다.")
                return redirect("plan_detail", pk=saved.pk)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "core/form.html", {"form": form, "heading": "초과 근무 계획", "is_plan": True, "preview": preview})


@login_required
def plan_detail(request, pk):
    plan = accessible_plan(request.user, pk)
    months = services.monthly_minutes(plan)
    scope = visible_to(OvertimePlan.objects.all(), request.user).values("pk")
    others = [p for p in OvertimePlan.objects.filter(Q(pk__in=scope) | Q(approver=request.user)).filter(user=plan.user).exclude(pk=pk) if months.keys() & services.monthly_minutes(p).keys()]
    record_scope = visible_to(WorkRecord.objects.all(), request.user).values("pk")
    records = WorkRecord.objects.filter(Q(pk__in=record_scope) | Q(plan__approver=request.user)).filter(user=plan.user)
    monthly_context = []
    for month in months:
        other_month_plans = [p for p in others if month in services.monthly_minutes(p)]
        status_totals = [{"label": label, "minutes": sum(services.monthly_minutes(p).get(month, 0) for p in [plan, *other_month_plans] if p.status == status)}
            for status, label in OvertimePlan.Status.choices if status in services.COUNTED]
        actual = sum(value["overtime"] for r in records for day, value in r.daily_minutes.items() if day.startswith(month))
        monthly_context.append({"month": month, "totals": status_totals, "actual": actual, "plans": other_month_plans})
    return render(request, "core/plan_detail.html", {"plan": plan,
        "events": display_events(plan.approval_events.select_related("actor"), OvertimePlan), "linked_records": records.filter(plan=plan),
        "monthly_context": monthly_context,
        "is_owner": plan.user_id == request.user.pk,
        "can_decide": services.can_decide(request.user, plan),
        "can_edit": plan.user_id == request.user.pk and plan.status in (OvertimePlan.Status.DRAFT, OvertimePlan.Status.REJECTED, OvertimePlan.Status.CONFIRMED, OvertimePlan.Status.APPROVED)})


@login_required
@require_POST
def plan_action(request, pk):
    accessible_plan(request.user, pk)
    form = DecisionForm(request.POST)
    if form.is_valid():
        try:
            services.transition_plan(request.user, pk, **form.cleaned_data)
            messages.success(request, "처리되었습니다.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    else:
        messages.error(request, "요청 값이 올바르지 않습니다. 새로고침 후 다시 시도하세요.")
    return redirect("plan_detail", pk=pk)


@login_required
def approval_inbox(request):
    plans = OvertimePlan.objects.filter(approver=request.user).select_related("user", "department")
    if not request.user.is_superuser:
        plans = plans.exclude(user=request.user)
    return render(request, "core/approvals.html", {"pending": plans.filter(status=OvertimePlan.Status.PENDING),
        "processed": plans.exclude(status=OvertimePlan.Status.PENDING)})


@login_required
def record_edit(request, pk=None):
    record = get_object_or_404(WorkRecord, pk=pk, user=request.user) if pk else WorkRecord(user=request.user)
    form = RecordForm(request.POST or None, instance=record, user=request.user)
    if request.method == "POST" and form.is_valid():
        try:
            saved = services.save_record(request.user, form.cleaned_data, pk=pk,
                version=form.cleaned_data["version"], change_reason=form.cleaned_data["change_reason"])
            messages.success(request, "근무 실적을 저장했습니다.")
            return redirect("record_detail", pk=saved.pk)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "core/form.html", {"form": form, "heading": "실제 근무 등록 · 수정", "is_plan": False})


@login_required
def record_detail(request, pk):
    scope = visible_to(WorkRecord.objects.all(), request.user).values("pk")
    record = get_object_or_404(WorkRecord.objects.select_related("user", "department", "plan").filter(
        Q(pk__in=scope) | Q(plan__approver=request.user)), pk=pk)
    return render(request, "core/record_detail.html", {"record": record, "events": display_events(record.events.select_related("actor"), WorkRecord)})


@login_required
@require_POST
def notification_read(request, pk):
    notification = get_object_or_404(Notification, pk=pk, user=request.user)
    notification.read = True
    notification.save(update_fields=["read"])
    return redirect("dashboard")


def visible_members(user):
    if user.is_superuser or user.role == User.Role.ADMIN:
        return User.objects.all()
    if user.role == User.Role.MANAGER and user.department_id:
        return User.objects.filter(Q(department=user.department) | Q(pk=user.pk))
    return User.objects.filter(pk=user.pk)


def require_administrator(user):
    if not (user.is_superuser or user.role == User.Role.ADMIN):
        raise PermissionDenied


@login_required
def setup(request):
    require_administrator(request.user)
    sync_state = HolidaySyncState.objects.filter(pk=1).first()
    affected = WorkRecord.objects.none()
    if sync_state and sync_state.changed_dates:
        days = set(sync_state.changed_dates)
        candidates = WorkRecord.objects.filter(
            started_at__date__lte=max(days), ended_at__date__gte=min(days)
        ).select_related("user").order_by("work_date").iterator()
        affected = []
        for record in candidates:
            if days.intersection(record.daily_minutes) or (not record.daily_minutes and str(record.work_date) in days):
                affected.append(record)
                if len(affected) == 100:
                    break
    return render(request, "core/setup.html", {"members": visible_members(request.user),
        "departments": Department.objects.select_related("head", "parent"),
        "policies": OvertimePolicy.objects.all(), "holidays": Holiday.objects.order_by("date"),
        "holiday_sync": sync_state, "holiday_affected_records": affected})


@login_required
def setup_edit(request, kind, pk=None):
    require_administrator(request.user)
    if kind == "member" and pk is None:
        raise Http404("사용자는 Microsoft 회사 계정으로 첫 로그인할 때 생성됩니다.")
    if kind == "approval":
        if not request.user.is_superuser:
            raise PermissionDenied
        ApprovalSettings.objects.get_or_create(pk=1)
        pk = 1
    configs = {"approval": (ApprovalSettings, ApprovalSettingsForm, "기본 결재 설정"), "member": (User, MemberForm, "사용자"), "department": (Department, DepartmentForm, "부서"),
        "policy": (OvertimePolicy, PolicyForm, "초과 근무 정책"), "holiday": (Holiday, HolidayForm, "휴일")}
    if kind not in configs:
        raise Http404
    model, form_class, title = configs[kind]
    obj = get_object_or_404(model, pk=pk) if pk else model()
    if kind == "member" and obj.is_superuser and not request.user.is_superuser:
        raise PermissionDenied
    form = form_class(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        from django.contrib.admin.models import LogEntry, ADDITION, CHANGE
        from django.db import transaction
        with transaction.atomic():
            obj = form.save(commit=False)
            if kind == "member":
                # Do not overwrite a concurrently incremented session revocation version.
                obj.save(update_fields=list(form._meta.fields))
                if "is_active" in form.changed_data:
                    from .authentication import revoke_sessions
                    revoke_sessions(obj.pk)
            else:
                obj.save()
            LogEntry.objects.log_actions(user_id=request.user.pk, queryset=[obj],
                action_flag=CHANGE if pk else ADDITION,
                change_message=f"기초정보 화면: {', '.join(form.changed_data)}")
        messages.success(request, f"{title} 정보를 저장했습니다.")
        return redirect("setup")
    return render(request, "core/form.html", {"form": form, "member_preview_id": obj.pk if kind == "member" else None, "heading": f"{title} {'수정' if pk else '등록'}", "is_setup": True})


def display_events(events, model):
    labels = {"user": "신청자", "department": "소속 부서", "status": "결재 상태", "approver": "승인자",
        "plan": "연결 계획", "source": "등록 출처", "version": "버전", "is_retrospective": "사후 등록",
        "unapproved_at_entry": "실적 등록 당시 사전 승인 미완료", "submitted_at": "제출 일시",
        "submitted_total_minutes": "제출 당시 월 누적(분)", "threshold_snapshot": "제출 당시 기준(분)"}
    omit = {"id", "updated_at", "created_at", "monthly_snapshot", "policy_snapshot", "daily_minutes", "external_id", "external_system"}
    result = list(events.order_by("-created_at", "-pk"))
    for item in result:
        before = item.snapshot.get("before") or {}
        after = item.snapshot.get("after") or {}
        item.changes = []
        for field in model._meta.fields:
            key = field.name
            if key in omit or before.get(key) == after.get(key):
                continue
            choices = dict(field.choices or ())
            def display(value):
                if value is None or value == "":
                    return "-"
                return choices.get(value, value)
            item.changes.append({"label": labels.get(key, str(field.verbose_name)),
                "before": display(before.get(key)), "after": display(after.get(key))})
    return result


@login_required
@require_POST
def holiday_sync(request):
    require_administrator(request.user)
    from .holidays import sync_holidays, SyncError
    try:
        count = sync_holidays()
        messages.success(request, f"{count}일 휴일을 동기화했습니다.")
    except SyncError as exc:
        messages.error(request, str(exc))
    return redirect("setup")


@login_required
def approver_preview(request, pk):
    require_administrator(request.user)
    member = get_object_or_404(User, pk=pk)
    if member.is_superuser and not request.user.is_superuser:
        raise PermissionDenied
    role = request.GET.get("role", member.role)
    if role not in User.Role.values:
        raise Http404
    department_id = request.GET.get("department", "")
    if department_id and not department_id.isdecimal():
        raise Http404
    member.department = get_object_or_404(Department, pk=department_id) if department_id else None
    member.role = role
    try:
        approver = services.default_approver(member)
        return JsonResponse({"label": f"자동 선택: {approver.display_name}"})
    except ValidationError as exc:
        return JsonResponse({"label": " / ".join(exc.messages)})
