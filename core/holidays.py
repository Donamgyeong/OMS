"""Read-only Graph holiday import. Existing attendance snapshots are never rewritten."""
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo
import time

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import Holiday, HolidaySource, HolidaySyncState


class SyncError(Exception):
    pass


def request_json(method, url, **kwargs):
    for attempt in range(3):
        try:
            response = requests.request(method, url, timeout=(5, 20), allow_redirects=False, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    time.sleep(1 + attempt)
                    continue
            if not 200 <= response.status_code < 300:
                raise SyncError(f"Microsoft 응답 오류 ({response.status_code})")
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            return data
        except requests.RequestException:
            if attempt == 2:
                raise SyncError("Microsoft 연결 실패") from None
        except ValueError:
            raise SyncError("Microsoft 응답 형식 오류") from None
    raise SyncError("Microsoft 연결 실패")


def fetch_events(start, end):
    values = [getattr(settings, 'HOLIDAY_' + key) for key in
              ('TENANT_ID', 'CLIENT_ID', 'CLIENT_SECRET', 'MAILBOX', 'CALENDAR_ID')]
    if not all(values):
        raise SyncError("휴일 캘린더 연결 설정이 필요합니다.")
    tenant, client, secret, mailbox, calendar = values
    try:
        UUID(tenant)
    except ValueError:
        raise SyncError("휴일 테넌트 ID 형식 오류") from None
    token = request_json('POST', f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token', data={
        'grant_type': 'client_credentials', 'client_id': client, 'client_secret': secret,
        'scope': 'https://graph.microsoft.com/.default'}).get('access_token')
    if not isinstance(token, str) or not token:
        raise SyncError("Microsoft 인증 응답 오류")
    url = f'https://graph.microsoft.com/v1.0/users/{quote(mailbox, safe="")}/calendars/{quote(calendar, safe="")}/calendarView'
    params = {'startDateTime': f'{start}T00:00:00+09:00', 'endDateTime': f'{end}T00:00:00+09:00', '$top': 1000}
    events, visited = [], set()
    while url:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.netloc != 'graph.microsoft.com'
                or not parsed.path.startswith('/v1.0/') or url in visited or len(visited) >= 1000):
            raise SyncError("Microsoft 페이지 링크 오류")
        visited.add(url)
        page = request_json('GET', url, headers={'Authorization': f'Bearer {token}',
            'Prefer': 'outlook.timezone="Asia/Seoul"'}, params=params)
        if not isinstance(page.get('value'), list):
            raise SyncError("Microsoft 일정 응답 오류")
        events.extend(page['value'])
        url, params = page.get('@odata.nextLink'), None
    return events


def event_dates(events, start, end):
    result = {}
    try:
        for item in events:
            if item.get('isCancelled') or not item.get('isAllDay'):
                continue
            # Graph returns the requested Asia/Seoul zone; honor explicit offsets as well.
            def local_day(value):
                dt = datetime.fromisoformat(value['dateTime'].replace('Z', '+00:00'))
                if dt.tzinfo:
                    dt = dt.astimezone(ZoneInfo('Asia/Seoul'))
                elif value.get('timeZone') not in ('Asia/Seoul', 'Korea Standard Time'):
                    raise ValueError
                return dt.date()
            first, last = local_day(item['start']), local_day(item['end'])
            if last <= first or not item['id']:
                raise ValueError
            day = max(first, start)
            while day < min(last, end):
                result[(item['id'], day)] = str(item.get('subject') or '휴일')[:100]
                day += timedelta(days=1)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SyncError("휴일 일정 형식 오류") from None
    return result


def sync_holidays():
    today = timezone.localdate()
    start, end = date(today.year - 1, 1, 1), date(today.year + 2, 1, 1)
    state, _ = HolidaySyncState.objects.get_or_create(pk=1)
    now = timezone.now()
    locked = HolidaySyncState.objects.filter(pk=1).filter(
        Q(running_until__isnull=True) | Q(running_until__lt=now)).update(running_until=now + timedelta(hours=2))
    if not locked:
        raise SyncError("휴일 동기화가 이미 실행 중입니다.")
    try:
        rows = event_dates(fetch_events(start, end), start, end)
        key = f'{settings.HOLIDAY_MAILBOX}/{settings.HOLIDAY_CALENDAR_ID}'
        with transaction.atomic():
            # A configured source replacement supersedes previous imported sources in this window.
            HolidaySource.objects.filter(date__gte=start, date__lt=end).delete()
            HolidaySource.objects.bulk_create([HolidaySource(calendar_key=key, event_id=event_id,
                date=day, name=name) for (event_id, day), name in rows.items()])
            before = dict(Holiday.objects.filter(date__gte=start, date__lt=end).values_list("date", "name"))
            names = {}
            for (_, day), name in sorted(rows.items()):
                names.setdefault(day, name)
            Holiday.objects.filter(is_manual=False, date__gte=start, date__lt=end).exclude(date__in=names).delete()
            for day, name in names.items():
                holiday, created = Holiday.objects.get_or_create(date=day, defaults={'name': name, 'is_manual': False})
                if not holiday.is_manual and not created:
                    holiday.name = name
                    holiday.save(update_fields=['name'])
            after = dict(Holiday.objects.filter(date__gte=start, date__lt=end).values_list("date", "name"))
            changed = [str(day) for day in sorted(before.keys() | after.keys()) if before.get(day) != after.get(day)]
            HolidaySyncState.objects.filter(pk=1).update(last_success=timezone.now(), last_error='', count=len(names), changed_dates=changed)
        return len(names)
    except Exception as exc:
        # Never persist exception bodies that might contain credentials or access tokens.
        message = str(exc) if isinstance(exc, SyncError) else '휴일 동기화 내부 오류'
        HolidaySyncState.objects.filter(pk=1).update(last_error=message[:200])
        raise SyncError(message) from None
    finally:
        HolidaySyncState.objects.filter(pk=1).update(running_until=None)
