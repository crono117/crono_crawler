from django import template
from leads.models import CATEGORIES

register = template.Library()

@register.filter
def service_label(value):
    return dict(CATEGORIES).get(value, value)
