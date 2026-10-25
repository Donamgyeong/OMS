import logging
from uuid import UUID

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import EntraIdentity, User


class Command(BaseCommand):
    help = "Grant OMS administrator rights to an existing, active SSO identity."

    def add_arguments(self, parser):
        parser.add_argument("object_id", type=UUID)
        parser.add_argument("--superuser", action="store_true", help="Also grant Django Admin access")

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.ENTRA_CONFIGURED:
            raise CommandError("Entra configuration is required.")
        identity = EntraIdentity.objects.filter(tenant_id=settings.ENTRA_TENANT_ID,
            object_id=options["object_id"]).first()
        if not identity:
            raise CommandError("User must first sign in with an assigned Microsoft company account.")
        user = User.objects.select_for_update().get(pk=identity.user_id)
        if not user.is_active:
            raise CommandError("Cannot grant access to an inactive account.")
        fields = {"role": User.Role.ADMIN, "onboarding_completed": True}
        if options["superuser"]:
            fields.update(is_staff=True, is_superuser=True)
        User.objects.filter(pk=user.pk).update(**fields)
        logging.getLogger("oms.authentication").info("admin_bootstrapped user_id=%s superuser=%s", user.pk, options["superuser"])
        self.stdout.write(self.style.SUCCESS(f"OMS administrator granted: user_id={user.pk}"))
