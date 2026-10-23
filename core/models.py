from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models


class Department(models.Model):
    name = models.CharField("부서명", max_length=100, unique=True)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    head = models.ForeignKey("User", null=True, blank=True, on_delete=models.SET_NULL, related_name="headed_departments")
    active = models.BooleanField("활성", default=True)

    def __str__(self):
        return self.name


class User(AbstractUser):
    class Role(models.TextChoices):
        EMPLOYEE = "employee", "일반 사용자"
        MANAGER = "manager", "부서장"
        ADMIN = "admin", "관리자"

    employee_number = models.CharField("사번", max_length=30, unique=True, null=True, blank=True)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.PROTECT)
    role = models.CharField("역할", max_length=20, choices=Role.choices, default=Role.EMPLOYEE)


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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-work_date", "-pk"]
        indexes = [models.Index(fields=["user", "work_date", "status"])]

    def clean(self):
        super().clean()
        if self.start_time and self.end_time:
            start = self.start_time.hour * 60 + self.start_time.minute
            end = self.end_time.hour * 60 + self.end_time.minute
            minutes = end - start - (self.break_minutes or 0)
            if minutes <= 0:
                raise ValidationError("종료는 시작 이후여야 하며 휴게를 제외한 시간이 양수여야 합니다. 야간·월 경계는 날짜별로 나누어 등록하세요.")
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
        if self.plan_id and self.plan.user_id != self.user_id:
            raise ValidationError("다른 사용자의 계획을 연결할 수 없습니다.")
        if self.external_id and not self.external_system:
            raise ValidationError("외부 식별자가 있으면 외부 시스템을 지정하세요.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
