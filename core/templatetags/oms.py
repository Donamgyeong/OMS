from django import template

register = template.Library()


@register.filter
def duration(value):
    if value is None:
        return "-"
    minutes = int(value)
    hours, remainder = divmod(abs(minutes), 60)
    sign = "-" if minutes < 0 else ""
    return f"{sign}{hours}시간 {remainder}분" if hours else f"{sign}{remainder}분"
