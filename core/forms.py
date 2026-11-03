from django import forms
from django.utils import timezone
from .models import ApprovalSettings, Department, Holiday, OvertimePlan, OvertimePolicy, User, WorkRecord
from .services import PLAN_FIELDS, RECORD_FIELDS


class SectionedForm:
    @property
    def sections(self):
        layouts = {
            "PlanForm": [("근무 일정", ("work_date", "end_date", "start_time", "end_time", "break_minutes")),
                         ("업무 내용", ("title", "reason"))],
            "RecordForm": [("출퇴근 · 휴게", ("work_date", "started_at", "ended_at", "break_minutes")),
                           ("업무 · 연결 계획", ("plan", "description", "exception_reason", "change_reason"))],
            "MemberForm": [("기본 정보", ("first_name", "last_name", "email", "employee_number")),
                           ("소속 · 결재", ("department", "role", "designated_approver")),
                           ("근무 기준 · 상태", ("daily_work_minutes", "working_weekdays", "is_active", "onboarding_completed"))],
        }
        groups = layouts.get(type(self).__name__, [("상세 정보", tuple(self.fields))])
        return [(title, [self[name] for name in names if name in self.fields and not self[name].is_hidden])
                for title, names in groups]


class AttendanceWidget(forms.SplitDateTimeWidget):
    def __init__(self):
        super().__init__(date_format="%Y-%m-%d", time_format="%H:%M",
            date_attrs={"type": "date", "aria-label": "날짜"},
            time_attrs={"type": "time", "step": "60", "aria-label": "시간"})

    def decompress(self, value):
        if isinstance(value, str):
            from django.utils.dateparse import parse_datetime
            try:
                parsed = parse_datetime(value)
            except ValueError:
                parsed = None
            if parsed is None:
                return [value, None]
            value = parsed
        return super().decompress(value)

    def value_from_datadict(self, data, files, name):
        # Keep existing integrations posting one datetime value compatible.
        if name in data and name + "_0" not in data and name + "_1" not in data:
            return data.get(name)
        return super().value_from_datadict(data, files, name)


class AttendanceField(forms.SplitDateTimeField):
    widget = AttendanceWidget

    def clean(self, value):
        if isinstance(value, str):
            return forms.DateTimeField(input_formats=["%Y-%m-%dT%H:%M"]).clean(value)
        return super().clean(value)


class PlanForm(SectionedForm, forms.ModelForm):
    version = forms.IntegerField(widget=forms.HiddenInput, required=False)

    class Meta:
        model = OvertimePlan
        fields = PLAN_FIELDS
        widgets = {"work_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "end_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "start_time": forms.TimeInput(format="%H:%M", attrs={"type": "time", "step": "60"}),
            "end_time": forms.TimeInput(format="%H:%M", attrs={"type": "time", "step": "60"}),
            "reason": forms.Textarea(attrs={"rows": 4})}
        help_texts = {"end_date": "당일 근무는 비워 두세요. 야간·월 경계는 종료일을 지정하세요.",
            "break_minutes": "월별 계산 시 휴게는 근무 구간의 끝에서 차감합니다."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["version"].initial = self.instance.version
        elif not self.is_bound:
            self.initial.setdefault("work_date", timezone.localdate())


class RecordForm(SectionedForm, forms.ModelForm):
    started_at = AttendanceField(label="출근", input_date_formats=["%Y-%m-%d"], input_time_formats=["%H:%M"])
    ended_at = AttendanceField(label="퇴근", input_date_formats=["%Y-%m-%d"], input_time_formats=["%H:%M"])
    version = forms.IntegerField(widget=forms.HiddenInput, required=False)
    change_reason = forms.CharField(label="수정 사유", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    class Meta:
        model = WorkRecord
        fields = RECORD_FIELDS
        widgets = {"work_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 3}),
            "exception_reason": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.user = user
        self.instance.overtime_minutes = 0  # Calculated by the service after form validation.
        self.fields["plan"].queryset = OvertimePlan.objects.filter(user=user)
        for name, label in (("started_at", "출근"), ("ended_at", "퇴근")):
            widgets = self.fields[name].widget.widgets
            widgets[0].attrs["aria-label"] = label + " 날짜"
            widgets[1].attrs["aria-label"] = label + " 시간"
        if not self.instance.pk and not self.is_bound:
            today = timezone.localdate()
            self.initial.setdefault("work_date", today)
            self.initial.setdefault("started_at", [today, None])
            self.initial.setdefault("ended_at", [today, None])
        if self.instance.pk:
            self.fields["version"].initial = self.instance.version
            self.fields["change_reason"].required = True


class DecisionForm(forms.Form):
    version = forms.IntegerField(widget=forms.HiddenInput)
    action = forms.ChoiceField(choices=[(key, key) for key in ("approve", "reject", "recall", "cancel")], widget=forms.HiddenInput)
    comment = forms.CharField(label="결재 의견 / 반려 사유", required=False, max_length=4000, widget=forms.Textarea(attrs={"rows": 3}))


class MemberForm(SectionedForm, forms.ModelForm):
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


class DepartmentForm(SectionedForm, forms.ModelForm):
    class Meta:
        model = Department
        fields = ("name", "parent", "head", "active")


class PolicyForm(SectionedForm, forms.ModelForm):
    class Meta:
        model = OvertimePolicy
        fields = ("effective_from", "threshold_minutes")
        widgets = {"effective_from": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})}


class HolidayForm(SectionedForm, forms.ModelForm):
    def save(self, commit=True):
        self.instance.is_manual = True
        return super().save(commit=commit)

    class Meta:
        model = Holiday
        fields = ("date", "name")
        widgets = {"date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})}


class ApprovalSettingsForm(SectionedForm, forms.ModelForm):
    class Meta:
        model = ApprovalSettings
        fields = ('default_administrator',)
