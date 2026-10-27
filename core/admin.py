from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.views import redirect_to_login
from django.views.decorators.http import require_GET
from .authentication import revoke_sessions
from .models import ApprovalEvent, Department, Holiday, Notification, OvertimePlan, OvertimePolicy, User, WorkRecord, WorkRecordEvent
from .models import TeamsConversation, TeamsDelivery
from django.utils import timezone


class SuperuserOnly:
    # Business administrator role does not grant unrestricted Django admin access.
    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(User)
class OMSUserAdmin(SuperuserOnly, UserAdmin):
    fieldsets = (
        ("회사 계정", {"fields": ("display_name", "first_name", "last_name", "email", "entra_reference")}),
        ("접근 권한", {"fields": ("is_active", "is_staff", "is_superuser", "onboarding_completed")}),
        ("조직·근무 기준", {"fields": ("employee_number", "department", "role", "designated_approver", "daily_work_minutes", "working_weekdays")}),
    )
    readonly_fields = ("display_name", "entra_reference")
    list_display = ("display_name", "email", "employee_number", "department", "role", "is_active", "onboarding_completed")
    search_fields = ("first_name", "last_name", "email", "employee_number")
    ordering = ("first_name", "last_name", "email", "pk")

    @admin.display(description="이름", ordering="first_name")
    def display_name(self, obj):
        return obj.display_name

    @admin.display(description="Entra ID (tenant / object)")
    def entra_reference(self, obj):
        identity = getattr(obj, "entra_identity", None)
        return f"{identity.tenant_id} / {identity.object_id}" if identity else "SSO 연결 없음"

    def has_add_permission(self, request):
        return False

    def get_urls(self):
        return [url for url in super().get_urls() if not (url.name or "").endswith("_password_change")]

    def save_model(self, request, obj, form, change):
        obj.save(update_fields=[field.name for field in obj._meta.concrete_fields
            if not field.primary_key and field.name != "auth_version"])
        if "is_active" in form.changed_data:
            revoke_sessions(obj.pk)


@admin.register(Department)
class DepartmentAdmin(SuperuserOnly, admin.ModelAdmin):
    list_display = ("name", "parent", "head", "active")


@admin.register(OvertimePolicy)
class PolicyAdmin(SuperuserOnly, admin.ModelAdmin):
    list_display = ("effective_from", "threshold_minutes")


@admin.register(OvertimePlan)
class PlanAdmin(SuperuserOnly, admin.ModelAdmin):
    list_display = ("user", "work_date", "title", "duration_minutes", "status")
    list_filter = ("status", "department", "work_date")
    readonly_fields = tuple(field.name for field in OvertimePlan._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(WorkRecord)
class RecordAdmin(SuperuserOnly, admin.ModelAdmin):
    list_display = ("user", "work_date", "overtime_minutes", "source")
    list_filter = ("department", "source", "work_date")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ApprovalEvent)
class ApprovalAdmin(SuperuserOnly, admin.ModelAdmin):
    list_display = ("plan", "actor", "result", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Holiday)
class HolidayAdmin(SuperuserOnly, admin.ModelAdmin):
    def save_model(self, request, obj, form, change):
        obj.is_manual = True
        super().save_model(request, obj, form, change)

    list_display = ("date", "name")


@admin.register(WorkRecordEvent)
class WorkRecordEventAdmin(SuperuserOnly, admin.ModelAdmin):
    list_display = ("record", "actor", "reason", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


admin.site.site_header = "OMS 근무 관리"
admin.site.site_title = "OMS 관리자"


@admin.register(TeamsConversation)
class TeamsConversationAdmin(SuperuserOnly, admin.ModelAdmin):
    list_display = ("object_id", "tenant_id", "active", "updated_at")
    list_filter = ("active",)
    readonly_fields = tuple(field.name for field in TeamsConversation._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(TeamsDelivery)
class TeamsDeliveryAdmin(SuperuserOnly, admin.ModelAdmin):
    list_display = ("id", "recipient", "kind", "status", "attempts", "error_code", "created_at", "sent_at")
    list_filter = ("status", "kind")
    readonly_fields = tuple(field.name for field in TeamsDelivery._meta.fields)
    actions = ("retry_failed",)

    def has_add_permission(self, request):
        return False

    @admin.action(description="선택한 실패·설치 대기 알림 재시도")
    def retry_failed(self, request, queryset):
        for item in queryset.filter(status__in=[TeamsDelivery.Status.BLOCKED, TeamsDelivery.Status.FAILED]):
            changed = TeamsDelivery.objects.filter(pk=item.pk, status=item.status).update(
                status=TeamsDelivery.Status.PENDING, attempts=0, next_attempt_at=timezone.now(),
                error_code="", lease_token=None, lease_until=None)
            if changed:
                self.log_change(request, item, "Teams 알림 수동 재시도")


@require_GET
def sso_admin_login(request, extra_context=None):
    if request.user.is_authenticated:
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied("관리자 권한이 필요합니다.")
    return redirect_to_login(request.GET.get("next", "/admin/"))


admin.site.login = sso_admin_login


def no_password_change(request, *args, **kwargs):
    from django.http import Http404
    raise Http404("비밀번호는 Microsoft 회사 계정에서 관리합니다.")


admin.site.password_change = no_password_change
admin.site.password_change_done = no_password_change
