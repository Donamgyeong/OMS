from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def entra_deployment_checks(app_configs, **kwargs):
    if not settings.ENTRA_CONFIGURED:
        return [Error("Entra SSO configuration is required before deployment.", id="oms.E001")]
    return []
