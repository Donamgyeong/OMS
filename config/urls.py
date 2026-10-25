from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path
from core import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
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
    path("setup/<str:kind>/new/", views.setup_edit, name="setup_create"),
    path("setup/<str:kind>/<int:pk>/", views.setup_edit, name="setup_edit"),
]
