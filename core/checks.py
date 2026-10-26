from django.conf import settings
from django.core.checks import Error, Tags, register
from urllib.parse import urlsplit
from uuid import UUID


@register(Tags.security)
def teams_checks(app_configs, **kwargs):
    if not settings.TEAMS_ENABLED:
        return []
    errors = []
    for name in ("TEAMS_TENANT_ID", "TEAMS_CLIENT_ID"):
        try:
            UUID(getattr(settings, name))
        except (ValueError, TypeError, AttributeError):
            errors.append(Error(f"{name} must be a UUID when Teams is enabled.", id="oms.E002"))
    if not settings.TEAMS_CLIENT_SECRET:
        errors.append(Error("TEAMS_CLIENT_SECRET is required.", id="oms.E003"))
    try:
        uri = urlsplit(settings.OMS_PUBLIC_URL)
        valid_origin = (uri.scheme == "https" and uri.hostname in settings.ALLOWED_HOSTS and not any((uri.username,
            uri.password, uri.path, uri.query, uri.fragment)) and uri.port in (None, 443))
    except ValueError:
        valid_origin = False
    if not valid_origin:
        errors.append(Error("OMS_PUBLIC_URL must be an HTTPS origin on an allowed host.", id="oms.E004"))
    if settings.ENTRA_TENANT_ID and settings.TEAMS_TENANT_ID != settings.ENTRA_TENANT_ID:
        errors.append(Error("Teams and OMS must use the same tenant.", id="oms.E005"))
    return errors


@register(Tags.security, deploy=True)
def entra_deployment_checks(app_configs, **kwargs):
    if not settings.ENTRA_CONFIGURED:
        return [Error("Entra SSO configuration is required before deployment.", id="oms.E001")]
    return []
