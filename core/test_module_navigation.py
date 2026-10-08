from unittest.mock import Mock

from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse

from core.models import ModuleLink
from core.templatetags.module_navigation import module_header, resolve_module_links


class ModuleNavigationTests(TestCase):
    def render_header(self, user):
        context = module_header({"user": user})
        return render_to_string("core/_module_header.html", context)

    def test_customer_returns_to_customer_portal_and_sees_customer_modules(self):
        user = Mock(is_authenticated=True)
        user.groups.filter.return_value.exists.return_value = True
        html = self.render_header(user)
        self.assertIn(f'class="module-brand" href="{reverse("customer_home")}"', html)
        self.assertIn(reverse("distribution:customer_purchase"), html)
        self.assertNotIn(reverse("production:order_list"), html)
        user.groups.filter.assert_called_once_with(name="Buyer")

    def test_factory_returns_to_portal_and_sees_factory_modules(self):
        user = Mock(is_authenticated=True)
        user.groups.filter.return_value.exists.return_value = False
        html = self.render_header(user)
        self.assertIn(f'class="module-brand" href="{reverse("portal")}"', html)
        self.assertIn(reverse("production:order_list"), html)
        self.assertNotIn(reverse("distribution:customer_purchase"), html)

    def test_guest_returns_home_and_module_links_require_login(self):
        html = self.render_header(AnonymousUser())
        self.assertIn(f'class="module-brand" href="{reverse("home")}"', html)
        self.assertIn(f'{reverse("login")}?next=', html)
        self.assertEqual(html.count('id="module-drawer"'), 1)
        self.assertEqual(html.count('aria-controls="module-drawer"'), 1)


class ModuleLinkTests(TestCase):
    """resolve_module_links() must only return active rows with valid routes."""

    def setUp(self):
        # Tests start from an empty table regardless of the seeded defaults.
        ModuleLink.objects.all().delete()

    def test_resolve_module_links_skips_unknown_routes(self):
        ModuleLink.objects.create(label="Geçersiz", route="app:missing", sort_order=1)
        ModuleLink.objects.create(label="Üretim", route="production:order_list", sort_order=2)
        links = resolve_module_links()
        self.assertEqual([entry["module"].label for entry in links], ["Üretim"])
        self.assertEqual(links[0]["url"], reverse("production:order_list"))

    def test_resolve_module_links_ignores_inactive_rows(self):
        ModuleLink.objects.create(label="Pasif", route="production:order_list", is_active=False)
        self.assertEqual(resolve_module_links(), [])

    def test_resolve_module_links_orders_by_sort_order(self):
        ModuleLink.objects.create(label="B", route="production:order_list", sort_order=2)
        ModuleLink.objects.create(label="A", route="quality:quality_check_list", sort_order=1)
        links = resolve_module_links()
        self.assertEqual([entry["module"].label for entry in links], ["A", "B"])
