from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.template.loader import get_template
from django.test import Client, SimpleTestCase, override_settings
from django.urls import reverse


@override_settings(SECURE_SSL_REDIRECT=False)
class SharedThemeTests(SimpleTestCase):
    def test_public_and_admin_login_share_one_localized_controller(self):
        for language, label in (("tr", "Açık temaya geç"), ("en", "Switch to light theme")):
            self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
            for name in ("home", "login", "register", "admin:login"):
                with self.subTest(language=language, page=name):
                    response = self.client.get(reverse(name))
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, 'data-theme-toggle hidden', count=1)
                    self.assertContains(response, label)
                    self.assertContains(response, 'js/theme.js', count=1)
                    self.assertContains(response, 'css/theme.css', count=1)
                    self.assertNotContains(response, 'admin/js/theme.js')
                    content = response.content.decode()
                    self.assertLess(content.index('js/theme.js'), content.index('</head>'))

    def test_theme_assets_are_available_to_staticfiles(self):
        for name in ("js/theme.js", "css/theme.css", "css/admin-theme.css"):
            with self.subTest(asset=name):
                self.assertIsNotNone(finders.find(name))

    def test_template_migration_preserves_custom_tag_libraries(self):
        # Compile every application page, including pages needing custom filters.
        for path in Path(settings.BASE_DIR).glob("*/templates/*/*.html"):
            with self.subTest(template=path.name):
                get_template("/".join(path.parts[-2:]))

    def test_language_post_keeps_csrf_protection_and_shared_theme(self):
        client = Client(enforce_csrf_checks=True)
        response = client.get(reverse("home"))
        token = response.cookies[settings.CSRF_COOKIE_NAME].value
        data = {"language": "en", "next": reverse("home")}
        self.assertEqual(client.post(reverse("set_language"), data).status_code, 403)
        data["csrfmiddlewaretoken"] = token
        response = client.post(reverse("set_language"), data, follow=True)
        self.assertContains(response, "Switch to light theme")
        self.assertContains(response, "Choose Your Portal")
        self.assertContains(response, 'href="%s?next=%s"' % (reverse("login"), reverse("portal")))
