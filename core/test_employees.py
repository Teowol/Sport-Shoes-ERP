from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Employee


@override_settings(SECURE_SSL_REDIRECT=False)
class EmployeeListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="employee-owner")
        cls.owner.groups.add(Group.objects.create(name="FactoryOwner"))
        cls.buyer = get_user_model().objects.create_user(username="employee-buyer")
        cls.buyer.groups.add(Group.objects.create(name="Buyer"))
        cls.employee = Employee.objects.create(
            code="EMP-001", full_name="Ayşe Yılmaz", profession="Üretim Mühendisi",
            department="Üretim",
        )
        cls.inactive = Employee.objects.create(
            code="EMP-002", full_name="Mehmet Kaya", profession="Bakım Teknisyeni",
            department="Bakım", is_active=False,
        )

    def test_anonymous_user_must_login(self):
        response = self.client.get(reverse("employee_list"))
        self.assertRedirects(
            response, f'{reverse("login")}?next={reverse("employee_list")}',
            fetch_redirect_response=False,
        )

    def test_factory_owner_can_see_employees_and_navigation(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("employee_list"))
        self.assertContains(response, self.employee.full_name)
        self.assertContains(response, self.employee.profession)
        self.assertContains(response, "Pasif")
        self.assertContains(response, 'id="module-drawer"', count=1)
        self.assertContains(response, f'class="module-brand" href="{reverse("portal")}"')
        self.assertContains(self.client.get(reverse("portal")), reverse("employee_list"))

    def test_customers_and_unassigned_users_cannot_view_employee_records(self):
        unassigned = get_user_model().objects.create_user(username="unassigned")
        for user in (self.buyer, unassigned):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse("employee_list"))
                self.assertEqual(response.status_code, 403)
                self.assertNotContains(response, self.employee.full_name, status_code=403)
        self.client.force_login(self.buyer)
        self.assertNotContains(self.client.get(reverse("customer_home")), reverse("employee_list"))

    def test_staff_can_view_but_buyer_role_takes_precedence(self):
        staff = get_user_model().objects.create_user(username="employee-staff", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("employee_list")).status_code, 200)
        staff.groups.add(Group.objects.get(name="Buyer"))
        self.assertEqual(self.client.get(reverse("employee_list")).status_code, 403)

    def test_search_by_name_code_profession_and_department(self):
        self.client.force_login(self.owner)
        for query in ("Yılmaz", "EMP-001", "Mühendisi", "Üretim"):
            with self.subTest(query=query):
                response = self.client.get(reverse("employee_list"), {"q": query})
                self.assertContains(response, self.employee.full_name)
                self.assertNotContains(response, self.inactive.full_name)
        response = self.client.get(reverse("employee_list"), {"q": "not-found"})
        self.assertContains(response, "Aramanızla eşleşen çalışan bulunamadı.")

    def test_pagination_preserves_search_and_handles_invalid_page(self):
        Employee.objects.bulk_create([
            Employee(code=f"TEST-{i:03d}", full_name=f"Person {i:03d}", profession="Tester")
            for i in range(26)
        ])
        self.client.force_login(self.owner)
        response = self.client.get(reverse("employee_list"), {"q": "Tester", "page": 2})
        self.assertEqual(len(response.context["page_obj"]), 1)
        self.assertContains(response, "?q=Tester&amp;page=1")
        response = self.client.get(reverse("employee_list"), {"page": "invalid"})
        self.assertEqual(response.context["page_obj"].number, 1)

    def test_empty_list_and_english_labels(self):
        self.client.force_login(self.owner)
        Employee.objects.all().delete()
        self.assertContains(self.client.get(reverse("employee_list")), "Henüz çalışan kaydı bulunmuyor.")
        self.client.cookies["django_language"] = "en"
        response = self.client.get(reverse("employee_list"))
        self.assertContains(response, "Employees")
        self.assertContains(response, "No employees have been added yet.")
