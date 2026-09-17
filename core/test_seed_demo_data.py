from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from catalog.models import Color, ProductVariant, ShoeModel, Size
from distribution.models import Customer, SalesOrder, SalesOrderLine
from inventory.models import Lot, Product, Stock, StockMovement, Warehouse
from production.models import (
    BOMItem,
    BillOfMaterial,
    ProductionLine,
    ProductionOrder,
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
        Customer, SalesOrder, SalesOrderLine,
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
        self.assertEqual(create_movement.call_count, 3)
        expected_counts = {
            ShoeModel: 2, ProductVariant: 4, Product: 5,
            Stock: 3, Lot: 3, StockMovement: 3,
            ProductionOrder: 2, Customer: 2, SalesOrder: 2, SalesOrderLine: 4,
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
        for lot in Lot.objects.all():
            self.assertTrue(lot.barcode.startswith("LOT-"))
            self.assertEqual(lot.qr_code, lot.qr_payload)
            self.assertEqual(lot.remaining_quantity, lot.initial_quantity)
            stock = Stock.objects.get(product=lot.product, warehouse__code="DEMO-WH")
            self.assertEqual(stock.quantity, lot.initial_quantity)
            self.assertEqual(stock.available_quantity, stock.quantity)
            self.assertEqual(lot.stock_movements.get().product_id, lot.product_id)
        for order in ProductionOrder.objects.all():
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
        for order in SalesOrder.objects.all():
            self.assertEqual(order.status, SalesOrder.Status.DRAFT)
            self.assertEqual(order.total_amount, Decimal("4800"))
            self.assertEqual(order.lines.count(), 2)
        user = get_user_model().objects.get(username="DEMO-SEED")
        self.assertFalse(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.has_usable_password())

    def test_rerun_preserves_every_field_including_barcodes_and_timestamps(self):
        self.seed()
        before = self.snapshot()
        self.seed()
        self.assertEqual(before, self.snapshot())

    def test_rerun_does_not_reset_used_demo_data(self):
        self.seed()
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
