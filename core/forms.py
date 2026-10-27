from django import forms
from .models import ApprovalSettings, Department, Holiday, OvertimePlan, OvertimePolicy, User, WorkRecord
from .services import PLAN_FIELDS, RECORD_FIELDS


class PlanForm(forms.ModelForm):
    version = forms.IntegerField(widget=forms.HiddenInput, required=False)

    class Meta:
        model = OvertimePlan
        fields = PLAN_FIELDS
        widgets = {"work_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
            "reason": forms.Textarea(attrs={"rows": 4})}
        help_texts = {"end_date": "당일 근무는 비워 두세요. 야간·월 경계는 종료일을 지정하세요.",
            "break_minutes": "월별 계산 시 휴게는 근무 구간의 끝에서 차감합니다."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["version"].initial = self.instance.version


class RecordForm(forms.ModelForm):
    version = forms.IntegerField(widget=forms.HiddenInput, required=False)
    change_reason = forms.CharField(label="수정 사유", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    class Meta:
        model = WorkRecord
        fields = RECORD_FIELDS
        widgets = {"work_date": forms.DateInput(attrs={"type": "date"}),
            "started_at": forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
            "ended_at": forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
            "description": forms.Textarea(attrs={"rows": 3}),
            "exception_reason": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.user = user
        self.instance.overtime_minutes = 0  # Calculated by the service after form validation.
        self.fields["plan"].queryset = OvertimePlan.objects.filter(user=user)
        self.fields["started_at"].input_formats = ["%Y-%m-%dT%H:%M"]
        self.fields["ended_at"].input_formats = ["%Y-%m-%dT%H:%M"]
        if self.instance.pk:
            self.fields["version"].initial = self.instance.version
            self.fields["change_reason"].required = True


class DecisionForm(forms.Form):
    version = forms.IntegerField(widget=forms.HiddenInput)
    action = forms.ChoiceField(choices=[(key, key) for key in ("approve", "reject", "recall", "cancel")], widget=forms.HiddenInput)
    comment = forms.CharField(label="결재 의견 / 반려 사유", required=False, max_length=4000, widget=forms.Textarea(attrs={"rows": 3}))


class MemberForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "employee_number", "department", "role",
                  "is_active", "designated_approver", "daily_work_minutes", "working_weekdays", "onboarding_completed")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["designated_approver"]
        field.queryset = User.objects.filter(is_active=True)
        if not self.instance.is_superuser:
            field.queryset = field.queryset.exclude(pk=self.instance.pk)
        field.empty_label = "자동 선택 (일반 사용자: 부서장 / 부서장: 최고관리자 / 최고관리자: 본인)"
        field.help_text = "직접 지정하면 자동 선택보다 우선합니다. 부서·역할 변경 시 자동 선택은 새 기준을 적용합니다."
        if self.instance.pk:
            is_head = Department.objects.filter(head=self.instance).exists()
            if is_head != (self.instance.role == User.Role.MANAGER) and not self.instance.is_superuser:
                field.help_text += " 부서장 역할과 부서의 부서장 지정을 함께 확인하세요."
            from .services import resolve_approver
            from django.core.exceptions import ValidationError
            try:
                field.help_text += f" 현재 적용 승인자: {resolve_approver(self.instance)}"
            except ValidationError as exc:
                field.help_text += " " + " ".join(exc.messages)


class DepartmentForm(forms.ModelForm):
    class Meta:
        model = Department
        fields = ("name", "parent", "head", "active")


class PolicyForm(forms.ModelForm):
    class Meta:
        model = OvertimePolicy
        fields = ("effective_from", "threshold_minutes")
        widgets = {"effective_from": forms.DateInput(attrs={"type": "date"})}


class HolidayForm(forms.ModelForm):
    def save(self, commit=True):
        self.instance.is_manual = True
        return super().save(commit=commit)

    class Meta:
        model = Holiday
        fields = ("date", "name")
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}


class ApprovalSettingsForm(forms.ModelForm):
    class Meta:
        model = ApprovalSettings
        fields = ('default_administrator',)
