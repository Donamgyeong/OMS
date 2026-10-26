import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from core.teams import Connector, process_one


class Command(BaseCommand):
    help = "Process the durable Teams outbox (disabled unless TEAMS_ENABLED=1)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Drain currently due work and exit")
        parser.add_argument("--poll-seconds", type=int, default=5)

    def handle(self, *args, **options):
        if not settings.TEAMS_ENABLED:
            raise CommandError("Teams notifications are disabled.")
        if not 1 <= options["poll_seconds"] <= 60:
            raise CommandError("poll-seconds must be between 1 and 60")
        connector = Connector()
        try:
            while True:
                close_old_connections()
                if process_one(connector):
                    continue
                if options["once"]:
                    return
                time.sleep(options["poll_seconds"])
        except KeyboardInterrupt:
            return
