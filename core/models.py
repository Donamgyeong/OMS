from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from datetime import datetime, timedelta


class Department(models.Model):
    name = models.CharField("부서명", max_length=100, unique=True)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    head = models.ForeignKey("User", null=True, blank=True, on_delete=models.SET_NULL, related_name="headed_departments")
    active = models.BooleanField("활성", default=True)

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        visited = {self.pk} if self.pk else set()
        node = self.parent
        while node:
            if node.pk in visited:
                raise ValidationError("부서의 상하위 관계가 순환할 수 없습니다.")
            visited.add(node.pk)
            node = node.parent
        if self.head_id and (not self.head.is_active or self.head.department_id != self.pk):
            raise ValidationError("부서장은 해당 부서의 활성 사용자여야 합니다.")


class User(AbstractUser):
    class Role(models.TextChoices):
        EMPLOYEE = "employee", "일반 사용자"
        MANAGER = "manager", "부서장"
        ADMIN = "admin", "관리자"

    employee_number = models.CharField("사번", max_length=30, unique=True, null=True, blank=True)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.PROTECT)
    role = models.CharField("역할", max_length=20, choices=Role.choices, default=Role.EMPLOYEE)
    designated_approver = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="approval_members", verbose_name="지정 승인자")
    daily_work_minutes = models.PositiveIntegerField("일 소정근무(분)", default=480)
    working_weekdays = models.CharField("근무 요일(월=0, 일=6)", max_length=13, default="0,1,2,3,4")
    onboarding_completed = models.BooleanField("업무 설정 완료", default=False)
    auth_version = models.PositiveIntegerField(default=0, editable=False)

    def clean(self):
        super().clean()
        if self.designated_approver_id and self.designated_approver_id == self.pk:
            raise ValidationError("본인을 지정 승인자로 설정할 수 없습니다.")
        if not self.working_weekdays or any(day not in "0123456" or len(day) != 1 for day in self.working_weekdays.split(",")):
            raise ValidationError("근무 요일은 0~6을 쉼표로 구분하세요.")
        if self.onboarding_completed and not (self.is_superuser or self.role == self.Role.ADMIN):
            if not self.employee_number or not self.department_id or not self.department.active:
                raise ValidationError("업무 설정 완료 전 사번과 활성 부서를 지정하세요.")
            from .services import resolve_approver
            resolve_approver(self)


class EntraIdentity(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="entra_identity")
    tenant_id = models.UUIDField()
    object_id = models.UUIDField()
    linked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant_id", "object_id"], name="unique_entra_identity")]


class LoginAttempt(models.Model):
    # Only a digest is persisted here. Nonce and PKCE verifier live in the DB session.
    state_digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField(db_index=True)


class OvertimePolicy(models.Model):
    effective_from = models.DateField("적용 시작일", unique=True)
    threshold_minutes = models.PositiveIntegerField("무승인 기준(분)", default=960)

    class Meta:
        ordering = ["-effective_from"]

    def __str__(self):
        return f"{self.effective_from}: {self.threshold_minutes}분"


class OvertimePlan(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "임시 저장"
        CONFIRMED = "confirmed", "무승인 확정"
        PENDING = "pending", "승인 대기"
        APPROVED = "approved", "승인 완료"
        REJECTED = "rejected", "반려"
        CANCELLED = "cancelled", "취소"

    user = models.ForeignKey(User, on_delete=models.PROTECT)
    department = models.ForeignKey(Department, on_delete=models.PROTECT)
    work_date = models.DateField("근무 예정일")
    end_date = models.DateField("종료일", null=True, blank=True)
    start_time = models.TimeField("시작")
    end_time = models.TimeField("종료")
    break_minutes = models.PositiveIntegerField("휴게(분)", default=0)
    duration_minutes = models.PositiveIntegerField("계획 시간(분)", editable=False)
    title = models.CharField("업무명", max_length=200)
    reason = models.TextField("필요 사유")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    approver = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="assigned_plans")
    submitted_total_minutes = models.PositiveIntegerField(null=True, blank=True)
    threshold_snapshot = models.PositiveIntegerField(null=True, blank=True)
    policy_snapshot = models.JSONField(default=dict, blank=True)
    monthly_snapshot = models.JSONField(default=dict, blank=True)
    version = models.PositiveIntegerField(default=1)
    submitted_at = models.DateTimeField(null=True, blank=True)
    is_retrospective = models.BooleanField("사후 등록", default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-work_date", "-pk"]
        indexes = [models.Index(fields=["user", "work_date", "status"])]

    def clean(self):
        super().clean()
        if self.work_date and self.start_time and self.end_time:
            start = datetime.combine(self.work_date, self.start_time)
            end = datetime.combine(self.end_date or self.work_date, self.end_time)
            minutes = int((end - start).total_seconds() // 60) - (self.break_minutes or 0)
            if minutes <= 0:
                raise ValidationError("종료는 시작 이후여야 하며 휴게를 제외한 시간이 양수여야 합니다.")
            if (end - start) > timedelta(days=7):
                raise ValidationError("한 계획은 최대 7일까지 입력하세요.")
            self.duration_minutes = minutes

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.work_date} {self.title}"


class ApprovalEvent(models.Model):
    plan = models.ForeignKey(OvertimePlan, on_delete=models.PROTECT, related_name="approval_events")
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    result = models.CharField(max_length=20, choices=OvertimePlan.Status.choices)
    comment = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)
    snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class WorkRecord(models.Model):
    class Source(models.TextChoices):
        MANUAL = "manual", "수동"
        API = "api", "API"

    user = models.ForeignKey(User, on_delete=models.PROTECT)
    department = models.ForeignKey(Department, on_delete=models.PROTECT)
    work_date = models.DateField("근무일")
    started_at = models.DateTimeField("출근")
    ended_at = models.DateTimeField("퇴근")
    break_minutes = models.PositiveIntegerField("휴게(분)", default=0)
    overtime_minutes = models.PositiveIntegerField("실제 초과 근무(분)", default=0)
    duration_minutes = models.PositiveIntegerField("실제 근무(분)", default=0, editable=False)
    daily_minutes = models.JSONField(default=dict, blank=True)
    exception_reason = models.TextField("미계획·초과 수행 사유", blank=True)
    unapproved_at_entry = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    plan = models.ForeignKey(OvertimePlan, null=True, blank=True, on_delete=models.PROTECT)
    description = models.TextField("업무 내용", blank=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.MANUAL)
    external_system = models.CharField(max_length=50, blank=True)
    external_id = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-work_date", "-pk"]
        constraints = [models.UniqueConstraint(fields=["external_system", "external_id"],
            condition=~models.Q(external_id=""), name="unique_external_work_record")]

    def clean(self):
        super().clean()
        if self.started_at and self.ended_at:
            net = (self.ended_at - self.started_at).total_seconds() / 60 - (self.break_minutes or 0)
            if net <= 0 or self.overtime_minutes > net:
                raise ValidationError("실제 근무 시간과 초과 근무 시간을 확인하세요.")
            self.duration_minutes = int(net)
            if timezone.localtime(self.started_at).date() != self.work_date:
                raise ValidationError("근무일은 출근 날짜와 같아야 합니다.")
            if self.ended_at - self.started_at > timedelta(days=7):
                raise ValidationError("한 근무 기록은 최대 7일까지 입력하세요.")
        if self.plan_id and self.plan.user_id != self.user_id:
            raise ValidationError("다른 사용자의 계획을 연결할 수 없습니다.")
        if self.external_id and not self.external_system:
            raise ValidationError("외부 식별자가 있으면 외부 시스템을 지정하세요.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Holiday(models.Model):
    date = models.DateField("휴일", unique=True)
    name = models.CharField("이름", max_length=100)

    def __str__(self):
        return f"{self.date} {self.name}"


class WorkRecordEvent(models.Model):
    record = models.ForeignKey(WorkRecord, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    reason = models.TextField()
    snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    plan = models.ForeignKey(OvertimePlan, on_delete=models.PROTECT, null=True, blank=True)
    message = models.CharField(max_length=250)
    created_at = models.DateTimeField(auto_now_add=True)
    read = models.BooleanField(default=False)


class TeamsConversation(models.Model):
    tenant_id = models.UUIDField()
    object_id = models.UUIDField()
    bot_id = models.UUIDField()
    conversation_id = models.CharField(max_length=512)
    service_url = models.URLField(max_length=512)
    active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant_id", "object_id", "bot_id"], name="unique_teams_conversation")]


class TeamsDelivery(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "발송 대기"
        PROCESSING = "processing", "처리 중"
        SENT = "sent", "발송 완료"
        BLOCKED = "blocked", "설정/설치 확인 필요"
        FAILED = "failed", "재시도 한도 초과"
        SKIPPED = "skipped", "유효하지 않은 요청"

    event = models.ForeignKey(ApprovalEvent, on_delete=models.PROTECT)
    recipient = models.ForeignKey(User, on_delete=models.PROTECT)
    kind = models.CharField(max_length=20)
    payload = models.JSONField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    lease_until = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    activity_id = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "recipient", "kind"], name="unique_teams_delivery")]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]
