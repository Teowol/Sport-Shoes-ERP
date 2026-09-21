"""Authorization helpers for AI document management."""


def can_manage_documents(user) -> bool:
    """Allow trusted factory users while explicitly excluding buyers.

    Superusers remain an explicit override. A FactoryOwner still needs Django's
    ``is_staff`` flag to sign in to the admin site, but can use protected views.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    if user.groups.filter(name="Buyer").exists():
        return False
    return user.is_staff or user.groups.filter(name="FactoryOwner").exists()


def can_search_documents(user) -> bool:
    """Search follows private-document access and also requires an active user."""
    return bool(getattr(user, "is_active", False) and can_manage_documents(user))
