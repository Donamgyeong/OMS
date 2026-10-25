from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import ApprovalEvent, Department, Holiday, Notification, OvertimePlan, OvertimePolicy, User, WorkRecord, WorkRecordEvent


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
    fieldsets = UserAdmin.fieldsets + (("조직·근무 기준", {"fields": ("employee_number", "department", "role", "designated_approver", "daily_work_minutes", "working_weekdays")}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("조직·근무 기준", {"fields": ("employee_number", "department", "role", "designated_approver", "daily_work_minutes", "working_weekdays")}),)
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
