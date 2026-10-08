from django import template
from django.urls import NoReverseMatch, reverse

from core.models import ModuleLink
from core.permissions import can_view_employees

register = template.Library()


def resolve_module_links():
    """Return active ModuleLink rows with their reversed URLs.

    Entries whose `route` no longer resolves are skipped so a stale
    admin row can never break the portal or the module drawer.
    """
    links = []
    for module in ModuleLink.objects.filter(is_active=True):
        try:
            url = reverse(module.route)
        except NoReverseMatch:
            continue
        links.append({"module": module, "url": url})
    return links


@register.inclusion_tag("core/_module_header.html", takes_context=True)
def module_header(context):
    user = context.get("user")
    if not user or not user.is_authenticated:
        audience, portal_route = "guest", "home"
    elif user.groups.filter(name="Buyer").exists():
        audience, portal_route = "customer", "customer_home"
    else:
        audience, portal_route = "factory", "portal"
    return {
        "audience": audience, "portal_route": portal_route,
        "can_view_employees": audience == "factory" and can_view_employees(user),
        "module_links": resolve_module_links(),
    }
