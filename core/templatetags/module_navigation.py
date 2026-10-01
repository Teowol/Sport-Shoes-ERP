from django import template

register = template.Library()


@register.inclusion_tag("core/_module_header.html", takes_context=True)
def module_header(context):
    user = context.get("user")
    if not user or not user.is_authenticated:
        audience, portal_route = "guest", "home"
    elif user.groups.filter(name="Buyer").exists():
        audience, portal_route = "customer", "customer_home"
    else:
        audience, portal_route = "factory", "portal"
    return {"audience": audience, "portal_route": portal_route}
