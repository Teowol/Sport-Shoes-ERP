def can_view_employees(user):
    """Employee records belong to the factory area, never the customer portal."""
    return bool(
        user
        and user.is_authenticated
        and not user.groups.filter(name="Buyer").exists()
        and (
            user.is_staff
            or user.is_superuser
            or user.groups.filter(name="FactoryOwner").exists()
        )
    )
