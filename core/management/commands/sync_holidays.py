import time
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from core.holidays import sync_holidays, SyncError
from core.models import HolidaySyncState


class Command(BaseCommand):
    help = 'Microsoft 365 휴일 캘린더 동기화 (매일 03시 실행: --watch)'

    def add_arguments(self, parser):
        parser.add_argument('--watch', action='store_true', help='서울 시간 매일 03시 이후 실행, 실패 시 5분 후 재시도')

    def handle(self, *args, **options):
        if not options['watch']:
            return self.run_sync()
        while True:
            now = timezone.localtime()
            state = HolidaySyncState.objects.filter(pk=1).first()
            last_day = timezone.localtime(state.last_success).date() if state and state.last_success else None
            if now.hour >= 3 and last_day != now.date():
                try:
                    self.run_sync()
                except CommandError as exc:
                    self.stderr.write(str(exc))
                    time.sleep(300)
                    continue
            time.sleep(60)

    def run_sync(self):
        try:
            count = sync_holidays()
        except SyncError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(self.style.SUCCESS(f'{count}일 휴일 동기화 완료'))
