from datetime import datetime, time, timedelta
from django.db import migrations
from django.utils import timezone


def preserve_totals(apps, schema_editor):
    Record = apps.get_model("core", "WorkRecord")
    for record in Record.objects.using(schema_editor.connection.alias).iterator():
        start = timezone.localtime(record.started_at).replace(tzinfo=None)
        end = timezone.localtime(record.ended_at).replace(tzinfo=None) - timedelta(minutes=record.break_minutes)
        daily = {}
        while start < end:
            stop = min(end, datetime.combine(start.date() + timedelta(days=1), time.min))
            daily[str(start.date())] = {"worked": int((stop - start).total_seconds() // 60), "overtime": 0, "standard": None}
            start = stop
        # Preserve the manually entered overtime; never retroactively apply today's schedule.
        remaining = record.overtime_minutes
        for key in reversed(daily):
            allocated = min(remaining, daily[key]["worked"])
            daily[key]["overtime"] = allocated
            remaining -= allocated
        record.duration_minutes = sum(item["worked"] for item in daily.values())
        record.daily_minutes = daily
        record.save(update_fields=["duration_minutes", "daily_minutes"])


class Migration(migrations.Migration):
    dependencies = [("core", "0002_holiday_approvalevent_snapshot_approvalevent_version_and_more")]
    operations = [migrations.RunPython(preserve_totals, migrations.RunPython.noop)]
