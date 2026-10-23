from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from .models import OvertimePlan, WorkRecord
from .selectors import visible_to


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


@login_required
def dashboard(request):
    today = timezone.localdate()
    plans = visible_to(OvertimePlan.objects.select_related("user", "department"), request.user).filter(
        work_date__year=today.year, work_date__month=today.month)
    records = visible_to(WorkRecord.objects.select_related("user", "department"), request.user).filter(
        work_date__year=today.year, work_date__month=today.month)
    counted = [OvertimePlan.Status.CONFIRMED, OvertimePlan.Status.PENDING, OvertimePlan.Status.APPROVED]
    return render(request, "core/dashboard.html", {
        "month": today, "plans": plans[:20], "records": records[:20],
        "planned_minutes": plans.filter(status__in=counted).aggregate(total=Sum("duration_minutes"))["total"] or 0,
        "actual_minutes": records.aggregate(total=Sum("overtime_minutes"))["total"] or 0,
        "pending_count": plans.filter(status=OvertimePlan.Status.PENDING).count(),
    })
