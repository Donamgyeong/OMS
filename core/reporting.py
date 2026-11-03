"""Monthly reports use stored attendance snapshots, never today's holiday policy."""
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from io import BytesIO

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .models import OvertimePlan, WorkRecord, ReportAllowance, User
from .selectors import visible_to
from .services import monthly_minutes, COUNTED

MAX_RECORDS = 10000
MAX_DETAILS = 50000


def next_month(day):
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def months_between(start, end):
    while start <= end:
        yield start.strftime('%Y-%m')
        start = next_month(start)


@contextmanager
def snapshot():
    nested = connection.in_atomic_block
    with transaction.atomic():
        if connection.vendor == 'postgresql' and not nested:
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        yield


def attendance_months(record):
    first = timezone.localtime(record.started_at).date().replace(day=1)
    last = timezone.localtime(record.ended_at - timedelta(microseconds=1)).date().replace(day=1)
    return list(months_between(first, last))


def build_report(user, start, end, department=None, member=None):
    span = (end.year - start.year) * 12 + end.month - start.month
    if start.day != 1 or end.day != 1 or not 0 <= span < 12 or (end.year == 9999 and end.month == 12):
        raise ValidationError('조회 기간은 시작 월부터 최대 12개월로 지정하세요.')
    with snapshot():
        return _build_report(user, start, end, department, member)


def _build_report(user, start, end, department, member):
    months = set(months_between(start, end))
    stop = next_month(end)
    start_at = timezone.make_aware(datetime.combine(start, time.min))
    end_at = timezone.make_aware(datetime.combine(stop, time.min))
    all_plans = OvertimePlan.objects.filter(work_date__lt=stop).filter(Q(work_date__gte=start) | Q(end_date__gte=start))
    all_records = WorkRecord.objects.filter(started_at__lt=end_at, ended_at__gt=start_at)
    plans, records = visible_to(all_plans, user), visible_to(all_records, user)
    if department:
        plans, records = plans.filter(department=department), records.filter(department=department)
    if member:
        plans, records = plans.filter(user=member), records.filter(user=member)
    if plans.count() + records.count() > MAX_RECORDS:
        raise ValidationError('조회 자료가 10,000건을 넘습니다. 조회 기간이나 사용자를 줄여 주세요.')
    selected_plan_ids = list(plans.values_list('pk', flat=True))
    selected_record_ids = list(records.values_list('pk', flat=True))
    rows, plan_details, record_details, reviews = {}, [], [], []
    policies = list(ReportAllowance.objects.all())

    def get_row(owner, month):
        key = (owner.pk, month)
        if key not in rows:
            allowance = next((policy.included_minutes for policy in policies if str(policy.effective_month)[:7] <= month), None)
            rows[key] = dict(user_id=owner.pk, month=month, employee_number=owner.employee_number or '',
                name=owner.display_name, current_department=str(owner.department) if owner.department_id else '',
                departments=set(), confirmed=0, approved=0, pending=0, retrospective=0,
                worked=0, overtime=0, allowance=allowance, warnings=set(), partial=False, incomplete=False)
        return rows[key]

    linked_visible = set(records.filter(plan__isnull=False).values_list('plan_id', flat=True))
    for plan in plans.select_related('user__department', 'department', 'approver').order_by('user_id', 'work_date', 'pk'):
        start_time = timezone.make_aware(datetime.combine(plan.work_date, plan.start_time))
        retrospective = not plan.submitted_at or plan.submitted_at >= start_time
        for month, minutes in monthly_minutes(plan).items():
            if month not in months:
                continue
            row = get_row(plan.user, month)
            row['departments'].add(str(plan.department))
            if plan.status in COUNTED:
                if retrospective:
                    row['retrospective'] += minutes
                else:
                    row[plan.status] += minutes
                if plan.pk not in linked_visible:
                    row['warnings'].add('조회 범위 내 연결 실적 없음')
                if plan.submitted_at and retrospective != plan.is_retrospective:
                    row['warnings'].add('계획 사전·사후 표시 불일치')
            plan_details.append([month, plan.user_id, plan.user.employee_number or '', plan.user.display_name,
                str(plan.department), plan.pk, plan.version, minutes,
                f'{plan.work_date} {plan.start_time:%H:%M}', f'{plan.end_date or plan.work_date} {plan.end_time:%H:%M}',
                timezone.localtime(plan.submitted_at).isoformat() if plan.submitted_at else '',
                '미제출' if not plan.submitted_at else ('사후등록' if retrospective else '사전등록'),
                plan.get_status_display(), str(plan.approver) if plan.approver_id else '', plan.title])

    visible_plan_totals = dict(records.filter(plan__isnull=False).values('plan_id').annotate(total=Sum('overtime_minutes')).values_list('plan_id', 'total'))
    for record in records.select_related('user__department', 'department', 'plan').order_by('user_id', 'work_date', 'pk'):
        days = record.daily_minutes
        entries = []
        invalid = False
        if days:
            try:
                for day, values in days.items():
                    parsed = date.fromisoformat(day)
                    if str(parsed) != day:
                        raise ValueError
                    if any(type(values[k]) is not int or values[k] < 0 for k in ('worked', 'standard', 'overtime')):
                        raise ValueError
                    if values['overtime'] > values['worked'] or day[:7] not in attendance_months(record):
                        raise ValueError
                    entries.append((day, values))
                if sum(values['worked'] for _, values in entries) != record.duration_minutes or sum(values['overtime'] for _, values in entries) != record.overtime_minutes:
                    raise ValueError
            except (ValueError, KeyError, TypeError, AttributeError):
                invalid = True
                entries = []
        elif len(attendance_months(record)) == 1:
            entries = [(str(timezone.localtime(record.started_at).date()), {'worked': record.duration_minutes, 'overtime': record.overtime_minutes, 'standard': None})]
        else:
            invalid = True
        if invalid:
            for month in set(attendance_months(record)) & months:
                row = get_row(record.user, month)
                row['incomplete'] = True
                row['departments'].add(str(record.department))
                row['warnings'].add('일별 내역 누락·불일치: 월 배분 확인 필요')
                reviews.append([month, record.user_id, record.pk, '월 배분 불가: 일별 내역 누락·불일치'])
            record_details.append(['미배분', record.user_id, record.user.employee_number or '', record.user.display_name,
                str(record.department), record.pk, record.version, '', record.duration_minutes, '', record.overtime_minutes,
                record.break_minutes, record.plan_id, '월 배분 확인 필요', record.exception_reason, record.description, timezone.localtime(record.started_at).isoformat(), timezone.localtime(record.ended_at).isoformat()])
            continue
        for day, values in entries:
            month = day[:7]
            if month not in months:
                continue
            row = get_row(record.user, month)
            row['departments'].add(str(record.department))
            row['worked'] += values['worked']
            row['overtime'] += values['overtime']
            flags = []
            if values['standard'] is None:
                flags.append('과거 실적: 저장 총량 사용')
            if not record.plan_id and values['overtime']:
                flags.append('미계획 실적')
            if record.plan_id and visible_plan_totals.get(record.plan_id, 0) > record.plan.duration_minutes:
                flags.append('계획 초과 수행(조회 범위 기준)')
            if record.unapproved_at_entry:
                flags.append('등록 당시 사전 승인 미완료')
            if record.exception_reason:
                flags.append('예외 사유 확인')
            row['warnings'].update(flags)
            record_details.append([month, record.user_id, record.user.employee_number or '', record.user.display_name,
                str(record.department), record.pk, record.version, day, values['worked'], values['standard'], values['overtime'],
                record.break_minutes, record.plan_id, ' / '.join(flags), record.exception_reason, record.description, timezone.localtime(record.started_at).isoformat(), timezone.localtime(record.ended_at).isoformat()])

    # Only return a partial flag, never the amounts or identities of hidden records.
    user_ids = {key[0] for key in rows}
    if user_ids:
        hidden_plans = all_plans.filter(user_id__in=user_ids).exclude(pk__in=selected_plan_ids).only('user_id', 'work_date', 'end_date', 'start_time', 'end_time', 'break_minutes')
        for plan in hidden_plans.iterator():
            for month in set(monthly_minutes(plan)) & months:
                if (plan.user_id, month) in rows:
                    rows[(plan.user_id, month)]['partial'] = True
        hidden_records = all_records.filter(user_id__in=user_ids).exclude(pk__in=selected_record_ids).only('user_id', 'started_at', 'ended_at')
        for record in hidden_records.iterator():
            for month in set(attendance_months(record)) & months:
                if (record.user_id, month) in rows:
                    rows[(record.user_id, month)]['partial'] = True
    result = sorted(rows.values(), key=lambda row: (row['month'], row['employee_number'], row['user_id']))
    for row in result:
        row['departments'] = ', '.join(sorted(row['departments']))
        row['planned'] = row['confirmed'] + row['approved'] + row['pending']
        row['fixed'] = row['confirmed'] + row['approved']
        row['difference'] = None if row['incomplete'] else row['overtime'] - row['fixed']
        if row['partial']:
            row['warnings'].add('월 전체 확인 필요')
        if row['allowance'] is None:
            row['warnings'].add('포함 기준 미등록')
        valid = not row['partial'] and not row['incomplete'] and row['allowance'] is not None
        row['included'] = min(row['overtime'], row['allowance']) if valid else None
        row['extra'] = max(row['overtime'] - row['allowance'], 0) if valid else None
        row['warnings'] = ' / '.join(sorted(row['warnings']))
    if len(plan_details) + len(record_details) > MAX_DETAILS:
        raise ValidationError('상세 자료가 50,000행을 넘습니다. 조회 범위를 줄여 주세요.')
    return dict(rows=result, plans=plan_details, records=record_details, reviews=reviews,
        start=start.strftime('%Y-%m'), end=end.strftime('%Y-%m'), generated_at=timezone.localtime().isoformat(),
        total_extra=sum(row['extra'] for row in result if row['extra'] is not None),
        unconfirmed=sum(row['extra'] is None for row in result),
        department_id=department.pk if department else None, user_id=member.pk if member else None,
        scope_label='전체 열람 권한' if user.is_superuser or user.role == User.Role.ADMIN else
            ('본인 및 소속 부서 기록' if user.role == User.Role.MANAGER else '본인 기록'))


def export_xlsx(report):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from .templatetags.oms import duration
    workbook = Workbook()
    workbook.remove(workbook.active)

    def sheet(name, headers, values):
        ws = workbook.create_sheet(name)
        ws.append(headers)
        for row in values:
            ws.append([ILLEGAL_CHARACTERS_RE.sub('', value)[:32767] if isinstance(value, str) else value for value in row])
            for cell in ws[ws.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = 's'
        ws.freeze_panes = 'E2'
        ws.auto_filter.ref = ws.dimensions
        ws.row_dimensions[1].height = 42
        for cell in ws[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='142C47')
            cell.alignment = Alignment(wrap_text=True, vertical='center')
        for column in ws.columns:
            width = min(48, max(16, max(len(str(cell.value or '')) for cell in column) * 1.6))
            ws.column_dimensions[get_column_letter(column[0].column)].width = width
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical='top', wrap_text=True)
                if cell.row % 2 == 0:
                    cell.fill = PatternFill('solid', fgColor='F0F4FA')
        return ws

    metrics = [('confirmed', '사전 무승인 확정'), ('approved', '사전 승인 완료'), ('pending', '사전 승인 대기'),
        ('planned', '사전등록 계획'), ('fixed', '사전 확정 계획'), ('retrospective', '사후등록 계획'),
        ('worked', '실제 총 근무'), ('overtime', '실제 초과 근무'), ('allowance', '월 포함 기준'),
        ('included', '기준 내 초과 근무'), ('extra', '포함 기준 초과분'), ('difference', '계획 대비 차이')]
    headers = ['월', '사용자 ID', '사번', '이름', '현재 부서', '기록 부서']
    for _, title in metrics:
        headers += [title + '(분)', title + '(시간·분)']
    headers += ['검토 사항']
    summary = []
    for row in report['rows']:
        values = [row[key] for key in ('month', 'user_id', 'employee_number', 'name', 'current_department', 'departments')]
        for key, _ in metrics:
            values += [row[key], duration(row[key]) if row[key] is not None else '확인 필요']
        summary.append(values + [row['warnings']])
    sheet('월별 사용자 요약', headers, summary)
    sheet('계획 상세', ['월', '사용자 ID', '사번', '이름', '기록 부서', '계획 ID', '버전', '월 배분(분)',
        '근무 시작', '근무 종료', '제출 시각', '등록 구분', '상태', '결재자', '업무명'], report['plans'])
    sheet('실적 상세', ['월', '사용자 ID', '사번', '이름', '기록 부서', '실적 ID', '버전', '근무일',
        '실제 근무(분)', '소정근무(분)', '초과 근무(분)', '전체 실적 휴게(분·반복 합산 금지)', '연결 계획 ID', '검토 사항', '예외 사유', '업무 내용', '출근 시각', '퇴근 시각'], report['records'])
    notes = [['추출 시각', report['generated_at']], ['열람 범위', report['scope_label']],
        ['Excel 텍스트', 'Excel에서 지원하지 않는 제어문자는 제거하며 셀당 최대 32,767자로 제한합니다.'], ['조회 기간', report['start'] + ' ~ ' + report['end']],
        ['부서 필터 ID', report['department_id']], ['사용자 필터 ID', report['user_id']],
        ['자료 기준', '추출 시점의 저장 상태. 근무·휴일 기준을 소급 재계산하지 않음.'],
        ['초과분', '사용자별 월별 max(실제 초과 근무 - 월 포함 기준, 0). 월간 잔여 시간 이월 없음.'],
        ['사전 확정', '근무 시작 전 제출한 무승인 확정 + 승인 완료. 승인 대기·사후등록 제외.'],
        ['제외 계획', '임시 저장·반려·취소는 상세에만 표시하며 계획 합계에서 제외.'],
        ['부분·불완전 집계', '열람·필터 범위 일부 또는 월 배분 불가이면 초과분은 빈칸. 실제 시간은 조회 범위 내 배분 가능한 실적의 합계.'],
        ['확인 가능한 월의 초과분 합계(분)', report['total_extra']], ['초과분 미확정 사용자·월 수', report['unconfirmed']]]
    notes += [[f'검토: {month} 사용자 {uid} 실적 {rid}', reason] for month, uid, rid, reason in report['reviews']]
    sheet('집계 기준·검토 사항', ['항목', '내용'], notes)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
