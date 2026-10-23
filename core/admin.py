from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import ApprovalEvent, Department, OvertimePlan, OvertimePolicy, User, WorkRecord


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
    fieldsets = UserAdmin.fieldsets + (("조직", {"fields": ("employee_number", "department", "role")}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("조직", {"fields": ("employee_number", "department", "role")}),)
    list_display = ("username", "employee_number", "department", "role", "is_active")


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
    readonly_fields = ("status", "approver", "submitted_total_minutes", "threshold_snapshot", "created_at", "updated_at")

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser and (obj is None or obj.status == OvertimePlan.Status.DRAFT)


@admin.register(WorkRecord)
class RecordAdmin(SuperuserOnly, admin.ModelAdmin):
    list_display = ("user", "work_date", "overtime_minutes", "source")
    list_filter = ("department", "source", "work_date")


@admin.register(ApprovalEvent)
class ApprovalAdmin(SuperuserOnly, admin.ModelAdmin):
    list_display = ("plan", "actor", "result", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


admin.site.site_header = "OMS 근무 관리"
admin.site.site_title = "OMS 관리자"
