from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from distribution.models import Customer


@override_settings(SECURE_SSL_REDIRECT=False)
class HealthCheckTests(TestCase):
    def test_health_endpoint_returns_ok(self):
        response = self.client.get("/healthz/")

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"status": "ok"})


@override_settings(SECURE_SSL_REDIRECT=False)
class LanguageSwitchTests(SimpleTestCase):
    def test_home_is_english_only(self):
        response = self.client.get(reverse("home"))

        self.assertNotContains(response, 'class="language-toggle')
        self.assertContains(response, "Customer Login")
        self.assertContains(response, "Factory Owner Login")
        self.assertContains(response, '<html lang="en">', html=False)

    def test_login_and_register_have_no_language_toggle(self):
        for url_name in ("login", "register"):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))

                self.assertNotContains(response, 'class="language-toggle')
                self.assertContains(response, '<html lang="en">', html=False)


@override_settings(SECURE_SSL_REDIRECT=False)
class RolePortalLanguageTests(TestCase):
    def setUp(self):
        self.user_model = get_user_model()
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"

    def test_factory_portal_uses_selected_english_language(self):
        user = self.user_model.objects.create_user(
            username="factory-language-test",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("portal"))

        self.assertContains(response, "ERP Modules")
        self.assertContains(response, "Inventory Management")
        self.assertContains(response, '<html lang="en">', html=False)
        self.assertNotContains(response, "ERP Modülleri")

    def test_customer_portal_uses_selected_english_language(self):
        user = self.user_model.objects.create_user(
            username="customer-language-test",
            password="test-password",
            first_name="Test",
            last_name="Customer",
            email="customer@example.com",
        )
        buyer_group = Group.objects.create(name="Buyer")
        user.groups.add(buyer_group)
        Customer.objects.create(
            user=user,
            code="CUST-LANG",
            name="Test Customer",
            email=user.email,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("customer_home"))

        self.assertContains(response, "Customer Operations")
        self.assertContains(response, "Product Catalog")
        self.assertContains(response, '<html lang="en">', html=False)
        self.assertNotContains(response, "Müşteri İşlemleri")
