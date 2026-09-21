import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from distribution.models import Customer, SalesOrder, SalesOrderLine
from inventory.models import Product, Stock, Warehouse

from ai.services.assistant import LLMService, MAX_TOOL_ROUNDS
from ai.tools import (
    TOOL_FUNCTIONS,
    get_sales_orders,
    get_stock_by_product,
    search_products,
)


class ReadOnlyToolAccessTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        buyer_group = Group.objects.create(name="Buyer")

        self.buyer = user_model.objects.create(username="buyer", password="safe-password")
        self.buyer.groups.add(buyer_group)
        self.other_buyer = user_model.objects.create(username="other-buyer", password="safe-password")
        self.other_buyer.groups.add(buyer_group)
        self.staff_user = user_model.objects.create(
            username="staff", password="safe-password", is_staff=True
        )
        self.unassigned_user = user_model.objects.create(
            username="unassigned", password="safe-password"
        )

        self.own_customer = Customer.objects.create(
            user=self.buyer, code="CUST-OWN", name="Kendi Müşteri", email="own@example.test"
        )
        self.other_customer = Customer.objects.create(
            user=self.other_buyer, code="CUST-OTHER", name="Diğer Müşteri", email="other@example.test"
        )
        self.finished_product = Product.objects.create(
            code="FG-001", name="Bitmiş Ürün", product_type=Product.ProductType.FINISHED_GOOD
        )
        self.raw_product = Product.objects.create(
            code="RM-001", name="Hammadde", product_type=Product.ProductType.RAW_MATERIAL
        )
        self.warehouse = Warehouse.objects.create(code="DEP-TEST", name="Test Deposu")
        Stock.objects.create(product=self.finished_product, warehouse=self.warehouse, quantity=Decimal("12.500"))

        own_order = SalesOrder.objects.create(customer=self.own_customer, order_number="SO-OWN")
        SalesOrderLine.objects.create(sales_order=own_order, product=self.finished_product, quantity=Decimal("2"), unit_price=Decimal("99"))
        other_order = SalesOrder.objects.create(customer=self.other_customer, order_number="SO-OTHER")
        SalesOrderLine.objects.create(sales_order=other_order, product=self.finished_product, quantity=Decimal("3"), unit_price=Decimal("199"))

    def test_buyer_can_only_read_own_sales_orders(self):
        result = get_sales_orders(self.buyer, limit=10)

        self.assertTrue(result["ok"])
        self.assertEqual([order["order_number"] for order in result["data"]], ["SO-OWN"])
        self.assertNotIn("unit_price", json.dumps(result))

    def test_buyer_cannot_read_factory_stock_and_sees_finished_products_only(self):
        stock_result = get_stock_by_product(self.buyer, "FG-001")
        product_result = search_products(self.buyer, "001")

        self.assertFalse(stock_result["ok"])
        self.assertEqual(stock_result["error"], "access_denied")
        self.assertEqual(stock_result["data"], [])
        self.assertEqual([product["code"] for product in product_result["data"]], ["FG-001"])

    def test_staff_can_access_every_factory_tool_and_payload_is_json_safe(self):
        for tool_name, tool in TOOL_FUNCTIONS.items():
            kwargs = {
                "search_products": {"query": "001"},
                "get_stock_by_product": {"code_or_name": "FG-001"},
                "get_lot_details": {"lot_code": "missing"},
                "get_production_orders": {},
                "get_sales_orders": {},
                "get_fire_records": {},
                "search_documents": {"query": "missing"},
            }[tool_name]
            result = tool(self.staff_user, **kwargs)
            self.assertTrue(result["ok"], tool_name)
            json.dumps(result)

        stock_payload = get_stock_by_product(self.staff_user, "FG-001")
        row = stock_payload["data"][0]
        self.assertEqual(row["quantity"], "12.500")
        self.assertIsInstance(row["updated_at"], str)
        rendered = json.dumps(stock_payload).lower()
        for forbidden in ("\"id\"", "cost", "price", "margin", "password", "hash"):
            self.assertNotIn(forbidden, rendered)

    def test_unassigned_user_gets_blocked_tool_result_and_endpoint_response(self):
        result = search_products(self.unassigned_user, "FG")
        self.assertFalse(result["ok"])
        self.assertEqual(result["data"], [])

        self.client.force_login(self.unassigned_user)
        response = self.client.post(
            reverse("ai:ask"), data=json.dumps({"prompt": "stok nedir?"}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("Traceback", response.json()["error"])


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "ai-rate-tests"}})
class AssistantServiceTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.staff_user = user_model.objects.create(
            username="assistant-staff", password="safe-password", is_staff=True
        )

    def _tool_call_response(self, name, arguments, call_id="call-1"):
        call = SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[call]))]
        )

    def _text_response(self, content):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))]
        )

    def _service_with_responses(self, responses):
        service = LLMService()
        completions = Mock()
        completions.create.side_effect = responses
        service.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        service.api_key = "test-key"
        return service, completions

    def test_service_sends_tool_result_back_to_llm(self):
        service, completions = self._service_with_responses([
            self._tool_call_response("search_products", {"query": "missing"}),
            self._text_response("Uygun ürün bulunamadı."),
        ])

        answer = service.ask("ürün ara", "system", self.staff_user)

        self.assertEqual(answer, "Uygun ürün bulunamadı.")
        self.assertEqual(completions.create.call_count, 2)
        second_request_messages = completions.create.call_args_list[1].kwargs["messages"]
        self.assertEqual(second_request_messages[-1]["role"], "tool")
        self.assertIn('"ok": true', second_request_messages[-1]["content"])

    def test_service_blocks_repeated_tool_arguments(self):
        service, completions = self._service_with_responses([
            self._tool_call_response("search_products", {"query": "same"}, "call-1"),
            self._tool_call_response("search_products", {"query": "same"}, "call-2"),
            self._text_response("Tamamlandı."),
        ])

        self.assertEqual(service.ask("ürün ara", "system", self.staff_user), "Tamamlandı.")
        second_request_messages = completions.create.call_args_list[2].kwargs["messages"]
        self.assertIn("duplicate_tool_call", second_request_messages[-1]["content"])

    def test_service_stops_after_three_tool_rounds(self):
        service, completions = self._service_with_responses([
            self._tool_call_response("search_products", {"query": "one"}, "call-1"),
            self._tool_call_response("search_products", {"query": "two"}, "call-2"),
            self._tool_call_response("search_products", {"query": "three"}, "call-3"),
        ])

        answer = service.ask("ürün ara", "system", self.staff_user)

        self.assertIn("güvenli", answer)
        self.assertEqual(completions.create.call_count, MAX_TOOL_ROUNDS)

    @patch("ai.views.LLMService")
    def test_endpoint_rate_limit_returns_generic_message(self, service_class):
        from ai.views import RATE_LIMIT_REQUESTS

        cache.clear()
        service_class.return_value.model = "test-model"
        service_class.return_value.ask.return_value = "yanıt"
        self.client.force_login(self.staff_user)
        payload = json.dumps({"prompt": "merhaba"})

        for _ in range(RATE_LIMIT_REQUESTS):
            response = self.client.post(reverse("ai:ask"), data=payload, content_type="application/json")
            self.assertEqual(response.status_code, 200)

        response = self.client.post(reverse("ai:ask"), data=payload, content_type="application/json")
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("cache", response.json()["error"].lower())
