"""Small, additive development dataset; existing records are never reset."""

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone

from catalog.models import Color, ProductVariant, ShoeModel, Size
from distribution.models import Customer, SalesOrder, SalesOrderLine
from inventory.models import Lot, Product, StockMovement, Warehouse
from production.models import (
    BOMItem,
    BillOfMaterial,
    ProductionLine,
    ProductionOrder,
    Routing,
    RoutingOperation,
    WorkCenter,
)


MARKER = "seed_demo_data:v1"


def demo_record(model, key, data, identity=()):
    """Use unique keys, preserve existing values, reject ownership/link collisions."""
    obj, created = model.objects.get_or_create(**key, defaults=data)
    if created:
        # Run after save so generated barcodes and calculated totals are validated.
        # The enclosing transaction rolls back the save if validation fails.
        obj.full_clean()
    else:
        fields = set(identity) | {
            name for name in data if model._meta.get_field(name).is_relation
        }
        if any(getattr(obj, name) != data[name] for name in fields):
            raise CommandError(
                f"Demo kaydı çakışıyor: {model.__name__} {key}. "
                "Mevcut kayıt değiştirilmedi."
            )
    return obj, created


class Command(BaseCommand):
    help = "Yalnızca DEBUG=True geliştirme/test ortamında küçük demo verisi oluşturur."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_demo_data yalnızca DEBUG=True ortamında çalışır.")
        try:
            with transaction.atomic():
                self.seed()
        except (ValidationError, IntegrityError, ValueError) as exc:
            raise CommandError(f"Demo verisi oluşturulamadı; tüm işlem geri alındı: {exc}") from exc
        self.stdout.write(self.style.SUCCESS(
            "Demo verisi hazır: 2 ayakkabı modeli, 4 varyant, 1 hammadde, "
            "3 lot/stok girişi, 2 üretim emri, 2 müşteri ve 2 satış siparişi. "
            "Mevcut kayıtlar korunur."
        ))

    def seed(self):
        now = timezone.now()
        user, _ = demo_record(
            get_user_model(), {"username": "DEMO-SEED"},
            {"first_name": MARKER, "is_active": False, "password": make_password(None)},
            identity=("first_name", "is_active"),
        )
        warehouse, _ = demo_record(
            Warehouse, {"code": "DEMO-WH"},
            {"name": "DEMO Deposu", "address": MARKER}, identity=("address",),
        )
        color, _ = demo_record(
            Color, {"code": "DEMO-BLACK"},
            {"name": "DEMO Siyah", "hex_code": "#000000"}, identity=("name",),
        )
        sizes = [demo_record(
            Size, {"size_value": f"DEMO-{value}"},
            {"description": MARKER}, identity=("description",),
        )[0] for value in (40, 41)]
        material, _ = demo_record(
            Product, {"code": "DEMO-RAW"},
            {"name": "DEMO Taban Malzemesi", "description": MARKER,
             "product_type": Product.ProductType.RAW_MATERIAL, "unit": Product.Unit.KG},
            identity=("description", "product_type", "unit"),
        )
        self.seed_stock(material, warehouse, "DEMO-LOT-RAW", Decimal("100"))
        production_line, _ = demo_record(
            ProductionLine, {"code": "DEMO-LINE"},
            {"name": "DEMO Üretim Hattı", "description": MARKER,
             "capacity_per_day": Decimal("50")}, identity=("description",),
        )
        work_center, _ = demo_record(
            WorkCenter, {"code": "DEMO-WORK"},
            {"name": "DEMO Montaj", "production_line": production_line,
             "capacity_per_hour": Decimal("10")}, identity=("name",),
        )

        for index, name in enumerate(("Koşu", "Yürüyüş"), start=1):
            shoe, _ = demo_record(
                ShoeModel, {"code": f"DEMO-SHOE-{index}"},
                {"name": f"DEMO {name}", "description": MARKER}, identity=("description",),
            )
            variants = []
            for size in sizes:
                sku = f"DEMO-SHOE-{index}-{size.size_value}"
                product, _ = demo_record(
                    Product, {"code": sku},
                    {"name": f"DEMO {name} {size.size_value}", "description": MARKER,
                     "product_type": Product.ProductType.FINISHED_GOOD, "unit": Product.Unit.PAIR},
                    identity=("description", "product_type", "unit"),
                )
                variant, _ = demo_record(
                    ProductVariant, {"sku": sku},
                    {"shoe_model": shoe, "color": color, "size": size,
                     "product": product, "price": Decimal("1200.00")},
                )
                variants.append(variant)
            product = variants[0].product
            self.seed_stock(product, warehouse, f"DEMO-LOT-{index}", Decimal("12"))

            bom, _ = demo_record(
                BillOfMaterial, {"product": product, "version": 1},
                {"name": f"DEMO Reçete {index}", "description": MARKER,
                 "status": BillOfMaterial.Status.ACTIVE, "output_quantity": Decimal("1")},
                identity=("description",),
            )
            demo_record(
                BOMItem, {"bill_of_material": bom, "component": material},
                {"quantity": Decimal("0.5"), "operation_sequence": 1, "description": MARKER},
                identity=("description",),
            )
            routing, _ = demo_record(
                Routing, {"product": product, "version": 1},
                {"name": f"DEMO Rota {index}", "description": MARKER,
                 "status": Routing.Status.ACTIVE}, identity=("description",),
            )
            demo_record(
                RoutingOperation, {"routing": routing, "sequence": 1},
                {"name": "DEMO Montaj", "work_center": work_center,
                 "cycle_time_minutes": Decimal("6"), "description": MARKER},
                identity=("description",),
            )
            production, created = demo_record(
                ProductionOrder, {"order_number": f"DEMO-PO-{index}"},
                {"product": product, "bill_of_material": bom, "routing": routing,
                 "production_line": production_line, "created_by": user,
                 "raw_materials_warehouse": warehouse, "finished_goods_warehouse": warehouse,
                 "planned_quantity": Decimal("10"), "planned_start_date": now,
                 "planned_end_date": now + timedelta(days=2),
                 "status": ProductionOrder.Status.PLANNED},
            )
            if created:
                production.create_components_from_bom()
                production.create_operations_from_routing()

            customer, _ = demo_record(
                Customer, {"code": f"DEMO-CUST-{index}"},
                {"name": f"DEMO Müşteri {index}", "email": f"demo{index}@example.invalid",
                 "address": MARKER}, identity=("address",),
            )
            order, created = demo_record(
                SalesOrder, {"order_number": f"DEMO-SO-{index}"},
                {"customer": customer, "note": MARKER, "status": SalesOrder.Status.DRAFT,
                 "requested_delivery_date": (now + timedelta(days=7)).date()}, identity=("note",),
            )
            if created:
                # Lines have no unique constraint; seed only with the new parent.
                for variant in variants:
                    line = SalesOrderLine.objects.create(
                        sales_order=order, product=variant.product,
                        quantity=Decimal("2"), unit_price=variant.price,
                    )
                    line.full_clean()

    def seed_stock(self, product, warehouse, lot_number, quantity):
        lot, created = demo_record(
            Lot, {"lot_number": lot_number},
            {"product": product, "initial_quantity": quantity,
             "reference_type": MARKER, "note": MARKER}, identity=("reference_type", "note"),
        )
        if created:
            # Do not replenish stock on reruns after users consume the demo lot.
            # The domain service owns stock updates and movement idempotency.
            if StockMovement.objects.filter(scan_reference=lot_number).exists():
                raise CommandError(f"Demo stok referansı çakışıyor: {lot_number}")
            movement = StockMovement.create_verified_movement(
                product=product, warehouse=warehouse, lot=lot, quantity=quantity,
                movement_type=StockMovement.MovementType.IN,
                scan_reference=lot_number, reference_type=MARKER, reference_id=lot.pk,
                note=MARKER,
            )
            movement.full_clean()
