from datetime import timedelta
from decimal import Decimal
import os

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Subquery, Sum
from django.utils import timezone

from .models import Invoice, SalesOrder


@shared_task
def notify_sales_order_confirmed(order_pk, language_code="tr"):
    """
    Faturayı oluşturur ve müşteriye PDF ekiyle tek kez gönderir.
    Aynı sipariş için paralel Celery görevleri çalışsa bile
    select_for_update sayesinde çift e-posta gönderilmez.
    """
    from django.core.mail import EmailMessage
    from django.db import transaction
    from distribution.views import build_invoice_pdf_bytes

    use_english = language_code.lower().startswith("en")

    with transaction.atomic():
        try:
            order = (
                SalesOrder.objects
                .select_for_update()
                .select_related("customer")
                .get(pk=order_pk)
            )
        except SalesOrder.DoesNotExist:
            return f"Sipariş bulunamadı: {order_pk}"

        customer = order.customer
        recipient = customer.email

        if not recipient:
            return "Müşteri e-posta adresi tanımlı değil."

        tax_rate = Decimal("20.00")
        tax_factor = tax_rate / Decimal("100")
        subtotal = order.total_amount
        tax_amount = subtotal * tax_factor
        total_with_tax = subtotal + tax_amount

        invoice, created = Invoice.objects.get_or_create(
            sales_order=order,
            defaults={
                "customer": customer,
                "issue_date": timezone.now().date(),
                "due_date": timezone.now().date() + timedelta(days=30),
                "subtotal": subtotal,
                "tax_rate": tax_rate,
                "tax_amount": tax_amount,
                "total_amount": total_with_tax,
                "status": Invoice.Status.ISSUED,
            },
        )

        # Do not send again if another task has already sent the email.
        if invoice.emailed_at:
            return (
                f"Fatura {invoice.invoice_number} daha önce gönderilmiş: "
                f"{invoice.emailed_at}"
            )

        pdf_bytes = build_invoice_pdf_bytes(invoice, language_code)

        if use_english:
            subject = f"Your Invoice - {invoice.invoice_number}"
            body = (
                f"Dear {customer.name},\n\n"
                f"Please find the invoice for order {order.order_number} attached.\n\n"
                f"Invoice number: {invoice.invoice_number}\n"
                f"Subtotal: {invoice.subtotal:.2f} TL\n"
                f"VAT: {invoice.tax_amount:.2f} TL\n"
                f"Grand total: {invoice.total_amount:.2f} TL\n\n"
                "Thank you for choosing us."
            )
        else:
            subject = f"Faturanız - {invoice.invoice_number}"
            body = (
                f"Sayın {customer.name},\n\n"
                f"{order.order_number} numaralı siparişinize ait "
                f"faturanız ektedir.\n\n"
                f"Fatura numarası: {invoice.invoice_number}\n"
                f"Ara toplam: {invoice.subtotal:.2f} TL\n"
                f"KDV: {invoice.tax_amount:.2f} TL\n"
                f"Genel toplam: {invoice.total_amount:.2f} TL\n\n"
                "Bizi tercih ettiğiniz için teşekkür ederiz."
            )

        email = EmailMessage(
            subject=subject,
            body=body,
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                "noreply@erp.local",
            ),
            to=[recipient],
        )

        email.attach(
            f"{invoice.invoice_number}.pdf",
            pdf_bytes,
            "application/pdf",
        )
        email.send(fail_silently=False)

        # The email is marked as sent inside the transaction.
        invoice.emailed_at = timezone.now()
        invoice.save(update_fields=["emailed_at", "updated_at"])

        status = "oluşturuldu" if created else "zaten mevcuttu"
        return (
            f"Fatura {status}: {invoice.invoice_number} | "
            f"E-posta gönderildi: {recipient}"
        )

def _allocate_fifo_lots(product, warehouse, quantity):
    """
    Ürün miktarını FIFO kuralıyla lotlardan ayırır.

    Dönen değer:
        [(lot, miktar), ...]

    Lot miktarı yetersizse ValueError oluşturur. Böylece sevkiyatın
    yanlış veya eksik lot bilgisiyle yapılması engellenir.
    """
    from inventory.models import Lot, StockMovement

    remaining = quantity
    allocations = []

    lot_ids = StockMovement.objects.filter(
        warehouse=warehouse, lot__isnull=False
    ).values("lot_id")
    lots = (
        Lot.objects.select_for_update()
        .filter(
            product=product, status=Lot.Status.ACTIVE,
            pk__in=Subquery(lot_ids),
        )
        .order_by("created_at", "pk")
    )

    for lot in lots:
        if remaining <= Decimal("0"):
            break

        incoming = (
            StockMovement.objects
            .filter(
                lot=lot,
                warehouse=warehouse,
                movement_type=StockMovement.MovementType.IN,
            )
            .aggregate(total=Sum("quantity"))["total"]
            or Decimal("0")
        )
        outgoing = (
            StockMovement.objects
            .filter(
                lot=lot,
                warehouse=warehouse,
                movement_type=StockMovement.MovementType.OUT,
            )
            .aggregate(total=Sum("quantity"))["total"]
            or Decimal("0")
        )

        lot_available = incoming - outgoing
        if lot_available <= Decimal("0"):
            continue

        allocated = min(lot_available, remaining)
        allocations.append((lot, allocated))
        remaining -= allocated

    if remaining > Decimal("0"):
        raise ValueError(
            f"{product} ürünü için lot bazlı stok yetersiz. "
            f"Eksik lot miktarı: {remaining}"
        )

    return allocations


@shared_task
def process_order_fulfillment_task(order_pk, user_id=None, language_code="tr"):
    """
    Müşteri siparişini asenkron olarak işler:
    - Depoda stok varsa rezerve edip kargo (Shipment) kaydı oluşturur.
    - Stok yetersizse eksik kısım için Üretim Emri (ProductionOrder) açar.
    """
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from inventory.models import Stock, StockMovement, Warehouse
    from logistics.models import Shipment
    from production.models import BillOfMaterial, ProductionLine, ProductionOrder, Routing
    import uuid

    User = get_user_model()
    user = User.objects.filter(pk=user_id).first() if user_id else None

    try:
        order = SalesOrder.objects.prefetch_related("lines__product").get(pk=order_pk)
    except SalesOrder.DoesNotExist:
        return f"Sipariş bulunamadı: {order_pk}"

    fg_wh = Warehouse.objects.filter(code="DEP-MM").first()
    raw_wh = Warehouse.objects.filter(code="DEP-HM").first()
    if not fg_wh:
        return "Mamul deposu (DEP-MM) bulunamadı."

    all_shipped = True

    with transaction.atomic():
        for line in order.lines.select_related("product"):
            stock, _ = Stock.objects.select_for_update().get_or_create(
                product=line.product,
                warehouse=fg_wh,
                defaults={"quantity": Decimal("0"), "reserved_quantity": Decimal("0")},
            )
            available = stock.available_quantity

            # Skip if this line already has a shipment or production order
            already_shipped = Shipment.objects.filter(sales_order_line=line).exists()
            already_in_production = ProductionOrder.objects.filter(
                reference_order_number=order.order_number, product=line.product
            ).exists()
            if already_shipped or already_in_production:
                if already_in_production:
                    all_shipped = False
                continue

            # Scenario 1: Enough stock in the warehouse -> ship directly
            if available >= line.quantity:
                # Deduct from stock and record the movement
                stock.quantity -= line.quantity
                stock.save(update_fields=["quantity", "updated_at"])

                allocations = _allocate_fifo_lots(line.product, fg_wh, line.quantity)
                for lot, allocated_quantity in allocations:
                    StockMovement.objects.create(
                        product=line.product, warehouse=fg_wh, lot=lot,
                        movement_type=StockMovement.MovementType.OUT,
                        quantity=allocated_quantity, reference_type="SalesOrder",
                        reference_id=order.pk,
                        note=f"Sipariş {order.order_number} FIFO sevkiyat çıkışı",
                    )

                # Create the shipment record
                shipment_no = f"SHP-{order.order_number}-{uuid.uuid4().hex[:4].upper()}"
                Shipment.objects.create(
                    shipment_number=shipment_no,
                    sales_order=order,
                    sales_order_line=line,
                    warehouse=fg_wh,
                    quantity=line.quantity,
                    status=Shipment.Status.SHIPPED,
                    shipped_at=timezone.now(),
                    note="Otomatik depo çıkışı ve kargolama",
                )

                line.shipped_quantity = line.quantity
                line.save(update_fields=["shipped_quantity"])
                continue

            # Scenario 2: Insufficient stock -> production order for the shortfall
            all_shipped = False
            shortfall = line.quantity - max(available, Decimal("0"))

            if available > 0:
                stock.reserved_quantity += available
                stock.save(update_fields=["reserved_quantity", "updated_at"])

            bom = BillOfMaterial.objects.filter(
                product=line.product, status=BillOfMaterial.Status.ACTIVE
            ).first()
            routing = Routing.objects.filter(
                product=line.product, status=Routing.Status.ACTIVE
            ).first()
            prod_line = ProductionLine.objects.filter(is_active=True).first()

            if not bom or not routing or not raw_wh or not prod_line:
                continue

            timestamp = timezone.now().strftime("%y%m%d%H%M%S")
            po_number = f"PO-{order.order_number}-{line.pk}-{timestamp[-4:]}"

            # Do not recreate an existing production order if one is already present
            po, po_created = ProductionOrder.objects.get_or_create(
                reference_order_number=order.order_number,
                product=line.product,
                defaults={
                    "order_number": po_number,
                    "bill_of_material": bom,
                    "routing": routing,
                    "production_line": prod_line,
                    "raw_materials_warehouse": raw_wh,
                    "finished_goods_warehouse": fg_wh,
                    "planned_quantity": shortfall,
                    "created_by": user or order.customer.user or User.objects.filter(is_superuser=True).first(),
                    "planned_start_date": timezone.now(),
                    "planned_end_date": order.promised_delivery_date or timezone.now(),
                    "status": ProductionOrder.Status.PLANNED,
                },
            )
            if po_created:
                po.create_components_from_bom()
                po.create_operations_from_routing()
                try:
                    po.release()
                    po.start_production()
                except ValueError:
                    pass

        if all_shipped:
            order.status = SalesOrder.Status.SHIPPED
        else:
            order.status = SalesOrder.Status.IN_PRODUCTION
        order.save(update_fields=["status", "updated_at"])

    if order.status == SalesOrder.Status.SHIPPED:
        notify_sales_order_confirmed.delay(order.pk, language_code)

    return f"Sipariş {order.order_number} işlendi. Durum: {order.status}"

@shared_task
def ship_fulfilled_production_task(production_order_pk, user_id=None, language_code="tr"):
    """Üretim tamamlanan ürünleri, bağlı sipariş varsa otomatik sevkiyat eder."""
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from inventory.models import Stock, StockMovement, Warehouse
    from logistics.models import Shipment
    from production.models import ProductionOrder
    import uuid

    User = get_user_model()

    try:
        po = ProductionOrder.objects.get(pk=production_order_pk)
    except ProductionOrder.DoesNotExist:
        return f"Üretim emri bulunamadı: {production_order_pk}"

    if not po.reference_order_number:
        return "Üretim emrinin sipariş referansı yok."

    try:
        order = SalesOrder.objects.get(order_number=po.reference_order_number)
    except SalesOrder.DoesNotExist:
        return f"İlişkili sipariş bulunamadı: {po.reference_order_number}"

    fg_wh = Warehouse.objects.filter(code="DEP-MM").first()
    if not fg_wh:
        return "Mamul deposu (DEP-MM) bulunamadı."

    line = order.lines.filter(product=po.product).first()
    if not line:
        return "Siparişte üretilen ürün için kalem bulunamadı."

    remaining = line.quantity - (line.shipped_quantity or Decimal("0"))
    if remaining <= 0:
        return "Kalem için sevkiyat edilecek kalmamış."

    with transaction.atomic():
        stock, _ = Stock.objects.select_for_update().get_or_create(
            product=po.product,
            warehouse=fg_wh,
            defaults={"quantity": Decimal("0"), "reserved_quantity": Decimal("0")},
        )

        if stock.available_quantity < remaining:
            return f"Yeterli stok yok: {stock.available_quantity} / {remaining}"

        stock.quantity -= remaining
        stock.reserved_quantity -= min(remaining, stock.reserved_quantity)
        stock.save(update_fields=["quantity", "reserved_quantity", "updated_at"])

        allocations = _allocate_fifo_lots(po.product, fg_wh, remaining)
        for lot, allocated_quantity in allocations:
            StockMovement.objects.create(
                product=po.product, warehouse=fg_wh, lot=lot,
                movement_type=StockMovement.MovementType.OUT,
                quantity=allocated_quantity, reference_type="SalesOrder",
                reference_id=order.pk,
                note=f"Üretim tamamlandı, FIFO sevkiyat: {order.order_number}",
            )

        shipment_no = f"SHP-{order.order_number}-{uuid.uuid4().hex[:4].upper()}"
        Shipment.objects.create(
            shipment_number=shipment_no,
            sales_order=order,
            sales_order_line=line,
            warehouse=fg_wh,
            quantity=remaining,
            status=Shipment.Status.SHIPPED,
            shipped_at=timezone.now(),
            note=f"Üretim emri {po.order_number} sonrası otomatik sevkiyat",
        )

        line.shipped_quantity = (line.shipped_quantity or Decimal("0")) + remaining
        line.save(update_fields=["shipped_quantity"])

        all_shipped = all(
            (l.shipped_quantity or Decimal("0")) >= l.quantity
            for l in order.lines.all()
        )
        if all_shipped:
            order.status = SalesOrder.Status.SHIPPED
        else:
            order.status = SalesOrder.Status.PARTIALLY_SHIPPED
        order.save(update_fields=["status", "updated_at"])

    if order.status == SalesOrder.Status.SHIPPED:
        notify_sales_order_confirmed.delay(order.pk, language_code)

    return f"Sipariş {order.order_number} kalem sevkiyat edildi. Durum: {order.status}"