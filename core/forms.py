from django import forms
from .models import Department, Holiday, OvertimePlan, OvertimePolicy, User, WorkRecord
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
    password = forms.CharField(label="비밀번호", required=False, widget=forms.PasswordInput,
        help_text="신규 계정은 필수입니다. 기존 계정은 비워 두면 유지됩니다.")

    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email", "employee_number", "department", "role",
                  "is_active", "designated_approver", "daily_work_minutes", "working_weekdays")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password"].required = not self.instance.pk
        self.fields["designated_approver"].queryset = User.objects.filter(is_active=True).exclude(pk=self.instance.pk)

    def clean_password(self):
        from django.contrib.auth.password_validation import validate_password
        password = self.cleaned_data.get("password")
        if password:
            # Use the entered identity when checking password similarity.
            identity = User(**{name: self.cleaned_data.get(name, "") for name in ("username", "first_name", "last_name", "email")})
            validate_password(password, identity)
        return password


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
    class Meta:
        model = Holiday
        fields = ("date", "name")
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}
