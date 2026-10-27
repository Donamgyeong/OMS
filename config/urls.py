from django.contrib import admin
from django.urls import path
from core import auth_views, views
from core.teams_views import messages as teams_messages

urlpatterns = [
    path("api/teams/messages/", teams_messages, name="teams_messages"),
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.login_page, name="login"),
    path("accounts/entra/login/", auth_views.entra_login, name="entra_login"),
    path("accounts/entra/callback/", auth_views.entra_callback, name="entra_callback"),
    path("accounts/onboarding/", auth_views.onboarding, name="onboarding"),
    path("accounts/logout/", auth_views.logout_view, name="logout"),
    path("health/", views.health, name="health"),
    path("", views.dashboard, name="dashboard"),
    path("plans/new/", views.plan_edit, name="plan_create"),
    path("plans/<int:pk>/", views.plan_detail, name="plan_detail"),
    path("plans/<int:pk>/edit/", views.plan_edit, name="plan_edit"),
    path("plans/<int:pk>/action/", views.plan_action, name="plan_action"),
    path("approvals/", views.approval_inbox, name="approvals"),
    path("records/new/", views.record_edit, name="record_create"),
    path("records/<int:pk>/", views.record_detail, name="record_detail"),
    path("records/<int:pk>/edit/", views.record_edit, name="record_edit"),
    path("notifications/<int:pk>/read/", views.notification_read, name="notification_read"),
    path("setup/", views.setup, name="setup"),
    path("setup/members/<int:pk>/approver/", views.approver_preview, name="approver_preview"),
    path("setup/holidays/sync/", views.holiday_sync, name="holiday_sync"),
    path("setup/<str:kind>/new/", views.setup_edit, name="setup_create"),
    path("setup/<str:kind>/<int:pk>/", views.setup_edit, name="setup_edit"),
]
