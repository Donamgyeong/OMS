from uuid import UUID

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.authentication import revoke_sessions
from core.models import EntraIdentity


class Command(BaseCommand):
    help = "Invalidate all OMS sessions for an Entra account on their next request."

    def add_arguments(self, parser):
        parser.add_argument("object_id", type=UUID)
        parser.add_argument("--deactivate", action="store_true", help="Also prevent future OMS logins")

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.ENTRA_CONFIGURED:
            raise CommandError("Entra configuration is required.")
        identity = EntraIdentity.objects.filter(tenant_id=settings.ENTRA_TENANT_ID,
            object_id=options["object_id"]).first()
        if not identity:
            raise CommandError("No matching OMS SSO account.")
        revoke_sessions(identity.user_id, deactivate=options["deactivate"])
        self.stdout.write(self.style.SUCCESS(f"Sessions invalidated: user_id={identity.user_id}"))
