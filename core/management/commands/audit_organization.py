from django.core.exceptions import ValidationError
from django.db.models import Q
from django.core.management.base import BaseCommand, CommandError
from core.models import Department, User
from core.organization import save_member, save_department


class Command(BaseCommand):
    help = '사용자 역할·부서장 불일치 점검. --repair는 담당자가 명확한 경우만 복구합니다.'

    def add_arguments(self, parser):
        parser.add_argument('--repair', action='store_true')
        parser.add_argument('--actor', type=int, help='복구를 수행하는 활성 최고관리자 사용자 ID')

    def handle(self, *args, **options):
        actor = None
        if options['repair']:
            actor = User.objects.filter(pk=options['actor'], is_active=True, is_superuser=True).first()
            if actor is None:
                raise CommandError('--repair에는 --actor <활성 최고관리자 ID>가 필요합니다.')
        issues = repairs = 0
        for department in Department.objects.select_related('head').order_by('pk'):
            managers = list(User.objects.filter(department=department, role=User.Role.MANAGER, is_active=True))
            head = department.head
            valid_head = head and department.active and head.is_active and head.department_id == department.pk
            consistent = ((not head and not managers) or (valid_head and
                ((head.role == User.Role.MANAGER and len(managers) == 1 and managers[0].pk == head.pk)
                 or (head.is_superuser and head.role == User.Role.ADMIN and not managers))))
            if consistent:
                continue
            issues += 1
            self.stdout.write(f'부서 ID={department.pk}: 부서장 ID={department.head_id}, 부서장 역할 사용자 ID={[m.pk for m in managers]}')
            if actor:
                try:
                    if not head and department.active and len(managers) == 1:
                        save_member(actor, managers[0])
                    elif valid_head and head.role == User.Role.EMPLOYEE and not managers:
                        save_department(actor, department)
                    else:
                        self.stdout.write('  수동 확인 필요: 담당자를 임의로 선택하거나 역할을 해제하지 않습니다.')
                        continue
                    repairs += 1
                except ValidationError as exc:
                    self.stdout.write('  복구 보류: ' + ' '.join(exc.messages))
        orphans = User.objects.filter(role=User.Role.MANAGER, is_active=True).filter(
            Q(department__isnull=True) | Q(department__active=False))
        for member in orphans:
            self.stdout.write(f'사용자 ID={member.pk}: 활성 부서장에게 활성 소속 부서가 없습니다. 수동 확인 필요')
        self.stdout.write(f'불일치 부서 {issues}개, 복구 {repairs}개, 사용자 확인 필요 {orphans.count()}명')
