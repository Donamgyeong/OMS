import logging
import re


class AuthQueryFilter(logging.Filter):
    """Strip OAuth callback query strings from Django development access logs."""
    def filter(self, record):
        record.msg = re.sub(r"(/accounts/entra/callback/)\?[^\s\"]*", r"\1?[redacted]", record.getMessage())
        record.args = ()
        return True
