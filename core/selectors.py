from django.db.models import Q
from .models import User


def visible_to(queryset, user):
    """Scope plans and records by their stored department, never by URL input."""
    if not user.is_authenticated:
        return queryset.none()
    if user.is_superuser or user.role == User.Role.ADMIN:
        return queryset
    if user.role == User.Role.MANAGER and user.department_id:
        return queryset.filter(Q(department_id=user.department_id) | Q(user=user))
    return queryset.filter(user=user)
