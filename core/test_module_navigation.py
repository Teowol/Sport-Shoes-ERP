from unittest.mock import Mock

from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.urls import reverse

from core.templatetags.module_navigation import module_header


class ModuleNavigationTests(SimpleTestCase):
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
