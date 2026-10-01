from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from catalog.models import Color, ProductVariant, ShoeModel, Size
from core.models import Employee
from distribution.models import Customer, Invoice, SalesOrder, SalesOrderLine
from inventory.models import Lot, Product, Stock, StockMovement, Warehouse
from logistics.models import Shipment
from procurement.models import Supplier, PurchaseRequest, PurchaseRequestLine, PurchaseOrder, PurchaseOrderLine
from quality.models import QualityCheck
from production.models import (
    BOMItem,
    BillOfMaterial,
    ProductionLine,
    ProductionOrder,
    ProductionCost,
    ProductionOrderComponent,
    ProductionOrderOperation,
    Routing,
    RoutingOperation,
    WorkCenter,
)


@override_settings(DEBUG=True)
class SeedDemoDataTests(TestCase):
    models = (
        get_user_model(), Color, Size, ShoeModel, ProductVariant, Product,
        Warehouse, Lot, Stock, StockMovement, ProductionLine, WorkCenter,
        BillOfMaterial, BOMItem, Routing, RoutingOperation, ProductionOrder,
        ProductionOrderComponent, ProductionOrderOperation,
        Customer, SalesOrder, SalesOrderLine, Employee, Supplier, PurchaseRequest,
        PurchaseRequestLine, PurchaseOrder, PurchaseOrderLine, QualityCheck,
        ProductionCost, Invoice, Shipment,
    )

    def seed(self):
        output = StringIO()
        call_command("seed_demo_data", stdout=output)
        return output.getvalue()

    def snapshot(self):
        return {model: list(model.objects.order_by("pk").values()) for model in self.models}

    def test_creates_small_valid_dataset_using_domain_rules(self):
        original_movement = StockMovement.create_verified_movement
        with patch.object(
            StockMovement, "create_verified_movement", wraps=original_movement,
        ) as create_movement:
            self.assertIn("Demo verisi hazır", self.seed())
        self.assertEqual(create_movement.call_count, 8)
        expected_counts = {
            ShoeModel: 2, ProductVariant: 4, Product: 5,
            Stock: 5, Lot: 5, StockMovement: 9,
            ProductionOrder: 4, Customer: 2, SalesOrder: 4, SalesOrderLine: 6, Employee: 8,
            Supplier: 2, PurchaseRequest: 3, PurchaseRequestLine: 3,
            PurchaseOrder: 2, PurchaseOrderLine: 2, QualityCheck: 2,
            ProductionCost: 2, Invoice: 2, Shipment: 2,
        }
        for model, count in expected_counts.items():
            with self.subTest(model=model.__name__):
                self.assertEqual(model.objects.count(), count)
        for model in self.models:
            for obj in model.objects.all():
                obj.full_clean()
        for shoe in ShoeModel.objects.all():
            self.assertEqual(shoe.variants.count(), 2)
        for variant in ProductVariant.objects.select_related("product"):
            self.assertEqual(variant.product.product_type, Product.ProductType.FINISHED_GOOD)
            self.assertEqual(variant.product.unit, Product.Unit.PAIR)
            self.assertTrue(variant.product.barcode.startswith("PRD-"))
            self.assertEqual(variant.product.qr_code, variant.product.qr_payload)
        for lot in Lot.objects.filter(lot_number__in=["DEMO-LOT-RAW", "DEMO-LOT-1", "DEMO-LOT-2"]):
            self.assertTrue(lot.barcode.startswith("LOT-"))
            self.assertEqual(lot.qr_code, lot.qr_payload)
            self.assertEqual(lot.remaining_quantity, lot.initial_quantity)
            stock = Stock.objects.get(product=lot.product, warehouse__code="DEMO-WH")
            self.assertEqual(stock.quantity, lot.initial_quantity)
            self.assertEqual(stock.available_quantity, stock.quantity)
            self.assertEqual(lot.stock_movements.get().product_id, lot.product_id)
        for order in ProductionOrder.objects.filter(order_number__startswith="DEMO-PO-"):
            self.assertEqual(order.status, ProductionOrder.Status.PLANNED)
            self.assertEqual(order.product_id, order.bill_of_material.product_id)
            self.assertEqual(order.product_id, order.routing.product_id)
            self.assertLess(order.planned_start_date, order.planned_end_date)
            component = order.order_components.get()
            self.assertEqual(component.required_quantity, Decimal("5"))
            self.assertEqual(component.consumed_quantity, Decimal("0"))
            operation = order.order_operations.get()
            self.assertEqual(operation.routing_operation.routing_id, order.routing_id)
            self.assertEqual(
                operation.routing_operation.work_center.production_line_id,
                order.production_line_id,
            )
        for order in SalesOrder.objects.filter(order_number__startswith="DEMO-SO-"):
            self.assertEqual(order.status, SalesOrder.Status.DRAFT)
            self.assertEqual(order.total_amount, Decimal("4800"))
            self.assertEqual(order.lines.count(), 2)
        user = get_user_model().objects.get(username="DEMO-SEED")
        self.assertFalse(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.has_usable_password())

    def test_operational_scenarios_balance_stock_quality_cost_and_invoices(self):
        self.seed()
        for stock in Stock.objects.all():
            incoming = stock.product.stock_movements.filter(
                warehouse=stock.warehouse, movement_type=StockMovement.MovementType.IN,
            ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
            outgoing = stock.product.stock_movements.filter(
                warehouse=stock.warehouse, movement_type=StockMovement.MovementType.OUT,
            ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
            self.assertEqual(stock.quantity, incoming - outgoing)
            self.assertGreaterEqual(stock.available_quantity, 0)
        self.assertEqual(Lot.objects.get(lot_number="DEMO-OPS-RAW").remaining_quantity, Decimal("90"))
        self.assertEqual(Lot.objects.get(lot_number="LOT-DEMO-RUN-1").remaining_quantity, Decimal("2"))
        completed = ProductionOrder.objects.get(order_number="DEMO-RUN-1")
        partial = ProductionOrder.objects.get(order_number="DEMO-RUN-2")
        self.assertEqual(completed.status, ProductionOrder.Status.COMPLETED)
        self.assertEqual(partial.status, ProductionOrder.Status.QUALITY_CHECK)
        self.assertEqual(partial.scrapped_quantity, partial.quality_checks.get().scrapped_quantity)
        self.assertEqual(partial.produced_quantity, partial.quality_checks.get().accepted_quantity)
        for cost in ProductionCost.objects.all():
            self.assertEqual(cost.produced_quantity, cost.production_order.produced_quantity)
            self.assertEqual(cost.total_cost, cost.raw_material_cost + cost.labor_cost + cost.machine_cost + cost.overhead_cost + cost.scrap_cost)
            self.assertEqual(cost.unit_cost * cost.produced_quantity, cost.total_cost)
        for invoice in Invoice.objects.all():
            self.assertEqual(invoice.subtotal, invoice.sales_order.total_amount)
            self.assertEqual(invoice.tax_amount, invoice.subtotal * invoice.tax_rate / 100)
            self.assertEqual(invoice.total_amount, invoice.subtotal + invoice.tax_amount)
            self.assertIsNone(invoice.emailed_at)
        for shipment in Shipment.objects.all():
            self.assertEqual(shipment.sales_order_line.sales_order_id, shipment.sales_order_id)
            self.assertEqual(shipment.quantity, shipment.sales_order_line.shipped_quantity)
            movement = StockMovement.objects.get(reference_type="shipment", reference_id=shipment.pk)
            self.assertEqual(movement.quantity, shipment.quantity)
            self.assertEqual(movement.product_id, shipment.sales_order_line.product_id)

    def test_expansion_preserves_existing_v1_demo_data(self):
        command = "core.management.commands.seed_demo_data.Command"
        with patch(f"{command}.seed_procurement"), patch(f"{command}.seed_operations"):
            self.seed()
        before = self.snapshot()
        self.seed()
        after = self.snapshot()
        for model, rows in before.items():
            for row in rows:
                self.assertIn(row, after[model])
        self.assertEqual(QualityCheck.objects.count(), 2)
        self.assertEqual(Invoice.objects.count(), 2)

    def test_rerun_preserves_edited_operational_records(self):
        self.seed()
        PurchaseRequest.objects.filter(request_number="DEMO-PR-1").update(status=PurchaseRequest.Status.CANCELLED)
        Invoice.objects.filter(invoice_number="DEMO-INV-1").update(status=Invoice.Status.PAID)
        QualityCheck.objects.filter(production_order__order_number="DEMO-RUN-2").update(rejection_reason="Updated")
        before = self.snapshot()
        self.seed()
        self.assertEqual(before, self.snapshot())

    def test_supplier_collision_rolls_back_the_whole_seed(self):
        Supplier.objects.create(code="DEMO-SUP-2", company_name="Existing supplier")
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, "çakışıyor"):
            self.seed()
        self.assertEqual(before, self.snapshot())

    def test_seed_does_not_queue_external_tasks(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            self.seed()
        self.assertEqual(callbacks, [])

    def test_invoice_collision_rolls_back_production_and_stock_effects(self):
        customer = Customer.objects.create(code="REAL-CUST", name="Existing customer", email="real@example.invalid")
        order = SalesOrder.objects.create(order_number="REAL-SALE", customer=customer)
        Invoice.objects.create(
            invoice_number="DEMO-INV-2", customer=customer, sales_order=order,
            issue_date=timezone.now().date(),
        )
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, "çakışıyor"):
            self.seed()
        self.assertEqual(before, self.snapshot())

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_factory_module_pages_display_demo_rows(self):
        self.seed()
        user = get_user_model().objects.create_user(username="demo-viewer", is_staff=True)
        self.client.force_login(user)
        for route, marker in (
            ("catalog:product_list", "DEMO-SHOE-1"),
            ("inventory:stock_level_list", "DEMO-RAW"),
            ("inventory:stock_movement_list", "DEMO"),
            ("inventory:lot_tracking_list", "DEMO-OPS-RAW"),
            ("inventory:fire_tracking_list", "DEMO-RUN-2"),
            ("production:order_list", "DEMO-RUN-1"),
            ("production:operation_list", "DEMO-RUN-2"),
            ("production:cost_list", "DEMO-RUN-1"),
            ("quality:quality_check_list", "DEMO-RUN-2"),
            ("procurement:purchase_request_list", "DEMO-PR-1"),
            ("distribution:sales_order_list", "DEMO-SHIP-SO-1"),
        ):
            with self.subTest(route=route):
                self.assertContains(self.client.get(reverse(route)), marker)

    def test_rerun_preserves_every_field_including_barcodes_and_timestamps(self):
        self.seed()
        before = self.snapshot()
        self.seed()
        self.assertEqual(before, self.snapshot())

    def test_rerun_does_not_reset_used_demo_data(self):
        self.seed()
        employee = Employee.objects.get(code="DEMO-EMP-001")
        employee.profession = "Üretim Müdürü"
        employee.is_active = False
        employee.save()
        lot = Lot.objects.get(lot_number="DEMO-LOT-1")
        StockMovement.create_verified_movement(
            product=lot.product, warehouse=Warehouse.objects.get(code="DEMO-WH"),
            lot=lot, quantity=Decimal("3"), movement_type=StockMovement.MovementType.OUT,
            scan_reference="demo-test-consumption",
        )
        ProductionOrder.objects.get(order_number="DEMO-PO-1").release()
        order = SalesOrder.objects.get(order_number="DEMO-SO-1")
        order.status = SalesOrder.Status.CANCELLED
        order.save()
        line = order.lines.first()
        line.quantity = Decimal("7")
        line.save()
        before = self.snapshot()
        self.seed()
        self.assertEqual(before, self.snapshot())

    def test_unrelated_existing_records_are_unchanged(self):
        Employee.objects.create(code="REAL-EMP", full_name="Existing employee", profession="Engineer")
        product = Product.objects.create(code="REAL-PRODUCT", name="Existing product")
        warehouse = Warehouse.objects.create(code="REAL-WH", name="Existing warehouse")
        Stock.objects.create(product=product, warehouse=warehouse, quantity=Decimal("17"))
        Customer.objects.create(code="REAL-CUST", name="Existing customer", email="real@example.invalid")
        before = self.snapshot()
        self.seed()
        after = self.snapshot()
        for model, rows in before.items():
            for row in rows:
                self.assertIn(row, after[model])

    def test_late_code_collision_rolls_back_all_demo_records(self):
        Customer.objects.create(
            code="DEMO-CUST-2", name="Existing customer", email="existing@example.invalid",
        )
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, "çakışıyor"):
            self.seed()
        self.assertEqual(before, self.snapshot())

    def test_employee_code_collision_preserves_existing_records(self):
        Employee.objects.create(code="DEMO-EMP-003", full_name="Existing employee", profession="Engineer")
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, "çakışıyor"):
            self.seed()
        self.assertEqual(before, self.snapshot())

    def test_sku_relation_collision_does_not_relink_existing_records(self):
        self.seed()
        variant = ProductVariant.objects.first()
        variant.product = Product.objects.create(code="REAL-PRODUCT", name="Existing product")
        variant.save()
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, "çakışıyor"):
            self.seed()
        self.assertEqual(before, self.snapshot())

    def test_stock_reference_collision_preserves_existing_stock(self):
        product = Product.objects.create(code="REAL-PRODUCT", name="Existing product")
        warehouse = Warehouse.objects.create(code="REAL-WH", name="Existing warehouse")
        StockMovement.create_verified_movement(
            product=product, warehouse=warehouse, quantity=Decimal("8"),
            movement_type=StockMovement.MovementType.IN, scan_reference="DEMO-LOT-RAW",
        )
        before = self.snapshot()
        with self.assertRaisesMessage(CommandError, "çakışıyor"):
            self.seed()
        self.assertEqual(before, self.snapshot())

    def test_validation_failure_rolls_back_saved_records(self):
        before = self.snapshot()
        with patch.object(SalesOrderLine, "full_clean", side_effect=ValidationError("Invalid line")):
            with self.assertRaisesMessage(CommandError, "tüm işlem geri alındı"):
                self.seed()
        self.assertEqual(before, self.snapshot())

    @override_settings(DEBUG=False)
    def test_disabled_outside_development_before_database_access(self):
        with self.assertNumQueries(0):
            with self.assertRaisesMessage(CommandError, "DEBUG=True"):
                self.seed()
