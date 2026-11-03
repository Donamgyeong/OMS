from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .forms import MonthlyReportForm
from .models import ReportDownload
from .reporting import build_report, export_xlsx


@login_required
@never_cache
@require_GET
def monthly_report(request):
    month = timezone.localdate().strftime('%Y-%m')
    params = request.GET.copy()
    params.setdefault('start_month', month)
    params.setdefault('end_month', month)
    form = MonthlyReportForm(params, user=request.user)
    report, page = None, None
    if form.is_valid():
        try:
            data = form.cleaned_data
            report = build_report(request.user, data['start_month'], data['end_month'], data['department'], data['member'])
            if request.GET.get('download') == 'xlsx':
                content = export_xlsx(report)
                ReportDownload.objects.create(user=request.user, filters={key: report[key] for key in
                    ('start', 'end', 'department_id', 'user_id', 'generated_at', 'unconfirmed')}, row_count=len(report['rows']))
                response = HttpResponse(content, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                response['Content-Disposition'] = f'attachment; filename="OMS_monthly_{report["start"]}_{report["end"]}.xlsx"'
                return response
            page = Paginator(report['rows'], 100).get_page(request.GET.get('page'))
        except ValidationError as exc:
            form.add_error(None, ' '.join(exc.messages))
    query = params.copy()
    query.pop('page', None)
    query.pop('download', None)
    return render(request, 'core/monthly_report.html', {'form': form, 'report': report, 'page': page, 'filter_query': query.urlencode()})
