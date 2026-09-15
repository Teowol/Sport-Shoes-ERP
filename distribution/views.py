from decimal import Decimal, InvalidOperation
import os
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import get_language

from catalog.models import ProductVariant
from core.translations import localize
from inventory.models import Product, Stock, Warehouse
from production.models import BillOfMaterial, ProductionLine, ProductionOrder, Routing

from .models import Customer, SalesOrder, SalesOrderLine, Invoice
from .tasks import notify_sales_order_confirmed

import io
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable, Image,
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from django.conf import settings
from django.http import HttpResponse


def is_buyer(user):
    return user.is_authenticated and user.groups.filter(name="Buyer").exists()


def sales_order_list(request):
    """Satış siparişlerini listele ve filtrele."""
    queryset = SalesOrder.objects.select_related("customer").prefetch_related("lines")

    status_filter = request.GET.get("status", "").strip()
    customer_filter = request.GET.get("customer", "").strip()
    query = request.GET.get("q", "").strip()

    if status_filter:
        queryset = queryset.filter(status=status_filter)

    if customer_filter:
        queryset = queryset.filter(customer_id=customer_filter)

    if query:
        queryset = queryset.filter(
            Q(order_number__icontains=query)
            | Q(customer__name__icontains=query)
            | Q(customer__code__icontains=query)
        )

    context = {
        "orders": queryset,
        "customers": Customer.objects.filter(is_active=True),
        "status_choices": SalesOrder.Status.choices,
        "current_status": status_filter,
        "current_customer": customer_filter,
        "current_query": query,
    }
    return render(request, "distribution/sales_order_list.html", context)


def sales_order_detail(request, pk):
    """Sipariş detayını göster."""
    order = get_object_or_404(
        SalesOrder.objects.prefetch_related("lines__product"),
        pk=pk,
    )

    # Her kalem için kullanılabilir mamul stoku
    warehouse = Warehouse.objects.filter(code="DEP-MM").first()
    stock_map = {}
    if warehouse:
        for line in order.lines.all():
            stock = Stock.objects.filter(
                product=line.product,
                warehouse=warehouse,
            ).first()
            stock_map[line.pk] = stock.available_quantity if stock else Decimal("0")

    context = {
        "order": order,
        "warehouse": warehouse,
        "stock_map": stock_map,
        "linked_production_orders": ProductionOrder.objects.filter(
            reference_order_number=order.order_number,
        ),
    }
    return render(request, "distribution/sales_order_detail.html", context)


def sales_order_create(request):
    """Yeni sipariş oluştur."""
    buyer_mode = is_buyer(request.user)
    locked_customer = None

    if buyer_mode:
        locked_customer = getattr(request.user, "customer_profile", None)
        if not locked_customer:
            messages.error(request, "Müşteri profiliniz bulunamadı.")
            return redirect("customer_home")
        customers = Customer.objects.filter(pk=locked_customer.pk)
    else:
        customers = Customer.objects.filter(is_active=True)

    products = Product.objects.filter(
        product_type=Product.ProductType.FINISHED_GOOD,
        is_active=True,
    )

    if request.method == "POST":
        customer_id = locked_customer.pk if buyer_mode else request.POST.get("customer")
        requested_delivery_date = request.POST.get("requested_delivery_date")
        promised_delivery_date = request.POST.get("promised_delivery_date")
        note = request.POST.get("note", "").strip()

        if not customer_id:
            messages.error(request, "Müşteri seçimi zorunludur.")
        else:
            with transaction.atomic():
                order = SalesOrder.objects.create(
                    customer_id=customer_id,
                    requested_delivery_date=requested_delivery_date or None,
                    promised_delivery_date=promised_delivery_date or None,
                    note=note,
                )

                line_count = int(request.POST.get("line_count", "0"))
                for i in range(1, line_count + 1):
                    product_id = request.POST.get(f"product_{i}")
                    quantity = request.POST.get(f"quantity_{i}")
                    unit_price = request.POST.get(f"unit_price_{i}")

                    if product_id and quantity and unit_price:
                        SalesOrderLine.objects.create(
                            sales_order=order,
                            product_id=product_id,
                            quantity=quantity,
                            unit_price=unit_price,
                        )

                if buyer_mode:
                    from .tasks import process_order_fulfillment_task
                    order_pk = order.pk
                    user_pk = request.user.pk
                    transaction.on_commit(
                        lambda language_code=get_language(): process_order_fulfillment_task.delay(order_pk, user_pk, language_code)
                    )

            messages.success(request, f"{order.order_number} numaralı sipariş başarıyla oluşturuldu.")
            if buyer_mode:
                return redirect("distribution:customer_order_detail", pk=order.pk)
            return redirect("distribution:sales_order_detail", pk=order.pk)

    context = {
        "customers": customers,
        "products": products,
        "today": timezone.now().date().isoformat(),
        "buyer_mode": buyer_mode,
        "locked_customer": locked_customer,
    }
    return render(request, "distribution/sales_order_create.html", context)


def sales_order_confirm(request, pk):
    """Siparişi onayla ve stok rezervasyonu yap."""
    order = get_object_or_404(SalesOrder, pk=pk)

    if order.status != SalesOrder.Status.DRAFT:
        messages.error(request, "Sadece taslak siparişler onaylanabilir.")
        return redirect("distribution:sales_order_detail", pk=order.pk)

    warehouse = Warehouse.objects.filter(code="DEP-MM").first()
    if not warehouse:
        messages.error(request, "Mamul deposu (DEP-MM) bulunamadı.")
        return redirect("distribution:sales_order_detail", pk=order.pk)

    with transaction.atomic():
        for line in order.lines.select_related("product"):
            stock = Stock.objects.select_for_update().filter(
                product=line.product,
                warehouse=warehouse,
            ).first()

            if not stock:
                messages.error(
                    request,
                    f"{line.product} için mamul deposunda stok kaydı bulunamadı.",
                )
                return redirect("distribution:sales_order_detail", pk=order.pk)

            if stock.available_quantity < line.quantity:
                messages.error(
                    request,
                    f"{line.product} için yeterli stok yok. "
                    f"Kullanılabilir: {stock.available_quantity}, Talep: {line.quantity}",
                )
                return redirect("distribution:sales_order_detail", pk=order.pk)

        for line in order.lines.select_related("product"):
            stock = Stock.objects.get(
                product=line.product,
                warehouse=warehouse,
            )
            stock.reserved_quantity += line.quantity
            stock.save()

        order.status = SalesOrder.Status.CONFIRMED
        order.save()

        # Sipariş onaylandığında müşteriye fatura mailini gönder
        transaction.on_commit(
            lambda language_code=get_language(): notify_sales_order_confirmed.delay(order.pk, language_code)
        )

    messages.success(request, "Sipariş onaylandı ve stok rezerve edildi. Fatura müşteriye gönderilecektir.")
    return redirect("distribution:sales_order_detail", pk=order.pk)


def sales_order_cancel(request, pk):
    """Siparişi iptal et ve rezervasyonları kaldır."""
    order = get_object_or_404(SalesOrder, pk=pk)

    if order.status in [SalesOrder.Status.SHIPPED, SalesOrder.Status.COMPLETED]:
        messages.error(request, "Sevk edilmiş veya tamamlanmış sipariş iptal edilemez.")
        return redirect("distribution:sales_order_detail", pk=order.pk)

    warehouse = Warehouse.objects.filter(code="DEP-MM").first()

    with transaction.atomic():
        if order.status in [SalesOrder.Status.CONFIRMED, SalesOrder.Status.IN_PRODUCTION, SalesOrder.Status.READY_TO_SHIP]:
            for line in order.lines.select_related("product"):
                if warehouse:
                    stock = Stock.objects.filter(
                        product=line.product,
                        warehouse=warehouse,
                    ).first()
                    if stock:
                        stock.reserved_quantity = max(
                            Decimal("0"),
                            stock.reserved_quantity - line.quantity,
                        )
                        stock.save()

        order.status = SalesOrder.Status.CANCELLED
        order.save()

    messages.success(request, "Sipariş iptal edildi.")
    return redirect("distribution:sales_order_detail", pk=order.pk)


def sales_order_edit(request, pk):
    """Taslak durumundaki siparişi ve kalemlerini düzenleme."""
    order = get_object_or_404(SalesOrder.objects.prefetch_related("lines"), pk=pk)

    if order.status != SalesOrder.Status.DRAFT:
        messages.error(request, "Sadece taslak durumundaki siparişler düzenlenebilir.")
        return redirect("distribution:sales_order_detail", pk=order.pk)

    customers = Customer.objects.filter(is_active=True)
    products = Product.objects.filter(
        product_type=Product.ProductType.FINISHED_GOOD,
        is_active=True,
    )

    if request.method == "POST":
        customer_id = request.POST.get("customer")
        requested_delivery_date = request.POST.get("requested_delivery_date")
        promised_delivery_date = request.POST.get("promised_delivery_date")
        note = request.POST.get("note", "").strip()

        if not customer_id:
            messages.error(request, "Müşteri seçimi zorunludur.")
        else:
            with transaction.atomic():
                order.customer_id = customer_id
                order.requested_delivery_date = requested_delivery_date or None
                order.promised_delivery_date = promised_delivery_date or None
                order.note = note
                order.save()

                # Mevcut kalemleri temizleyip yenilerini ekleyelim
                order.lines.all().delete()
                line_count = int(request.POST.get("line_count", "0"))
                for i in range(1, line_count + 1):
                    product_id = request.POST.get(f"product_{i}")
                    quantity = request.POST.get(f"quantity_{i}")
                    unit_price = request.POST.get(f"unit_price_{i}")

                    if product_id and quantity and unit_price:
                        SalesOrderLine.objects.create(
                            sales_order=order,
                            product_id=product_id,
                            quantity=quantity,
                            unit_price=unit_price,
                        )

            messages.success(request, "Sipariş başarıyla güncellendi.")
            return redirect("distribution:sales_order_detail", pk=order.pk)

    context = {
        "order": order,
        "customers": customers,
        "products": products,
    }
    return render(request, "distribution/sales_order_edit.html", context)


def create_production_order_from_line(request, line_pk):
    """Sipariş kaleminden doğrudan üretim emri oluşturma."""
    line = get_object_or_404(
        SalesOrderLine.objects.select_related("sales_order", "product"),
        pk=line_pk,
    )
    order = line.sales_order

    if order.status not in [SalesOrder.Status.CONFIRMED, SalesOrder.Status.IN_PRODUCTION]:
        messages.error(request, "Yalnızca onaylanmış veya üretimdeki siparişler için üretim emri oluşturulabilir.")
        return redirect("distribution:sales_order_detail", pk=order.pk)

    # Ürüne ait aktif BOM ve Routing bulalım
    bom = BillOfMaterial.objects.filter(product=line.product, status=BillOfMaterial.Status.ACTIVE).first()
    if not bom:
        messages.error(request, f"{line.product.name} için aktif bir Reçete (BOM) bulunamadı. Önce BOM tanımlamalısınız.")
        return redirect("distribution:sales_order_detail", pk=order.pk)

    routing = Routing.objects.filter(product=line.product, status=Routing.Status.ACTIVE).first()
    if not routing:
        messages.error(request, f"{line.product.name} için aktif bir Rota (Routing) bulunamadı. Önce Rota tanımlamalısınız.")
        return redirect("distribution:sales_order_detail", pk=order.pk)

    raw_wh = Warehouse.objects.filter(code="DEP-HM").first()
    fg_wh = Warehouse.objects.filter(code="DEP-MM").first()
    prod_line = ProductionLine.objects.filter(is_active=True).first()

    if not raw_wh or not fg_wh:
        messages.error(request, "Hammadde (DEP-HM) veya Mamul (DEP-MM) deposu eksik.")
        return redirect("distribution:sales_order_detail", pk=order.pk)

    # Otomatik benzersiz bir emir numarası üretelim
    timestamp = timezone.now().strftime("%y%m%d%H%M%S")
    po_number = f"PO-{order.order_number}-{line.pk}-{timestamp[-4:]}"

    with transaction.atomic():
        po = ProductionOrder.objects.create(
            order_number=po_number,
            product=line.product,
            bill_of_material=bom,
            routing=routing,
            production_line=prod_line,
            raw_materials_warehouse=raw_wh,
            finished_goods_warehouse=fg_wh,
            planned_quantity=line.quantity,
            reference_order_number=order.order_number,
            planned_start_date=timezone.now(),
            planned_end_date=order.promised_delivery_date or timezone.now(),
        )
        # BOM ve Routing operasyonlarını/komponentlerini oluştur
        po.create_components_from_bom()
        po.create_operations_from_routing()

        # Sipariş durumunu 'Üretimde' yap
        if order.status != SalesOrder.Status.IN_PRODUCTION:
            order.status = SalesOrder.Status.IN_PRODUCTION
            order.save()

    messages.success(request, f"{po.order_number} numaralı üretim emri ve operasyon adımları oluşturuldu.")
    return redirect("production:order_detail", pk=po.pk)



FONT_DIR = settings.BASE_DIR / "static" / "fonts"
pdfmetrics.registerFont(TTFont("DejaVuSans", str(FONT_DIR / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(FONT_DIR / "DejaVuSans-Bold.ttf")))

LOGO_PATH = str(settings.BASE_DIR / "static" / "branding" / "SPEEDERSLOGO.png")
BRAND_COLOR = colors.HexColor("#1a3c6e")
LIGHT_BG = colors.HexColor("#eef2f8")


def build_invoice_pdf_bytes(invoice, language_code=None):
    """Fatura kaydı için PDF byte içeriği oluşturur (indirme ve e-posta eki için)."""
    order = invoice.sales_order
    language_code = language_code or get_language()
    t = lambda value: localize(value, language_code)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
    )

    normal = ParagraphStyle("normal_tr", fontName="DejaVuSans", fontSize=9, leading=13)
    bold = ParagraphStyle("bold_tr", fontName="DejaVuSans-Bold", fontSize=9, leading=13)
    company_name_style = ParagraphStyle(
        "company_name", fontName="DejaVuSans-Bold", fontSize=13,
        leading=16, textColor=BRAND_COLOR,
    )
    title_style = ParagraphStyle(
        "title_tr", fontName="DejaVuSans-Bold", fontSize=24,
        leading=28, textColor=BRAND_COLOR, alignment=2,
    )
    small_muted = ParagraphStyle(
        "small_muted", fontName="DejaVuSans", fontSize=8,
        leading=11, textColor=colors.HexColor("#7f8c8d"),
    )
    section_label = ParagraphStyle(
        "section_label", fontName="DejaVuSans-Bold", fontSize=8,
        leading=11, textColor=BRAND_COLOR,
    )

    def lines_to_paragraphs(lines, first_style=bold, rest_style=normal):
        paras = []
        for i, line in enumerate(lines):
            if not line:
                continue
            paras.append(Paragraph(line, first_style if i == 0 else rest_style))
        return paras

    elements = []

    # --- Üst Başlık: Logo + Şirket Bilgisi + FATURA ---
    company_lines = []
    if settings.COMPANY_NAME:
        company_lines.append(settings.COMPANY_NAME)
    if settings.COMPANY_ADDRESS:
        company_lines.append(settings.COMPANY_ADDRESS)
    if settings.COMPANY_TAX_OFFICE or settings.COMPANY_TAX_NUMBER:
        company_lines.append(t(f"{settings.COMPANY_TAX_OFFICE} V.D. - VKN: {settings.COMPANY_TAX_NUMBER}".strip(" -")))
    if settings.COMPANY_PHONE:
        company_lines.append(t(f"Tel: {settings.COMPANY_PHONE}"))
    if settings.COMPANY_EMAIL:
        company_lines.append(t(f"E-posta: {settings.COMPANY_EMAIL}"))

    company_paras = lines_to_paragraphs(company_lines, first_style=company_name_style, rest_style=normal)

    if os.path.exists(LOGO_PATH):
        logo = Image(LOGO_PATH, width=2.4 * cm, height=2.4 * cm)
        left_cell_content = [[logo, Table([[p] for p in company_paras])]]
        left_cell = Table(left_cell_content, colWidths=[2.8 * cm, 8.2 * cm])
        left_cell.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    else:
        left_cell = Table([[p] for p in company_paras], colWidths=[11 * cm])

    title_para = Paragraph(t("FATURA"), title_style)
    header_table = Table([[left_cell, title_para]], colWidths=[11.5 * cm, 6 * cm])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 0.4 * cm))
    elements.append(HRFlowable(width="100%", color=BRAND_COLOR, thickness=1.5))
    elements.append(Spacer(1, 0.7 * cm))

    # --- Müşteri Bilgisi + Fatura Bilgisi ---
    customer = invoice.customer
    customer_lines = [customer.name]
    if getattr(customer, "tax_number", None):
        customer_lines.append(t(f"VKN: {customer.tax_number}"))
    if getattr(customer, "address", None):
        customer_lines.append(customer.address)
    if getattr(customer, "phone", None):
        customer_lines.append(t(f"Tel: {customer.phone}"))
    if getattr(customer, "email", None):
        customer_lines.append(t(f"E-posta: {customer.email}"))

    customer_block = [[Paragraph(t("SAYIN"), section_label)]] + [[p] for p in lines_to_paragraphs(customer_lines)]
    customer_table = Table(customer_block, colWidths=[9 * cm])
    customer_table.setStyle(TableStyle([
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))

    invoice_info = [
        [Paragraph(t("Fatura No"), small_muted), Paragraph(invoice.invoice_number, bold)],
        [Paragraph(t("Fatura Tarihi"), small_muted), Paragraph(str(invoice.issue_date), normal)],
        [Paragraph(t("Vade Tarihi"), small_muted), Paragraph(str(invoice.due_date or "-"), normal)],
        [Paragraph(t("Sipariş No"), small_muted), Paragraph(order.order_number, normal)],
        [Paragraph(t("Durum"), small_muted), Paragraph(t(invoice.get_status_display()), normal)],
    ]
    invoice_info_table = Table(invoice_info, colWidths=[3.2 * cm, 4.3 * cm])
    invoice_info_table.setStyle(TableStyle([
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))

    top_info = Table([[customer_table, invoice_info_table]], colWidths=[9.5 * cm, 8 * cm])
    top_info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    elements.append(top_info)
    elements.append(Spacer(1, 0.9 * cm))

    # --- Ürün Tablosu ---
    lines = order.lines.select_related("product").all()
    line_data = [[t("Ürün"), t("Miktar"), t("Birim Fiyat"), t("Toplam")]]
    for line in lines:
        line_data.append([
            str(line.product),
            str(line.quantity),
            f"{line.unit_price:.2f} TL",
            f"{line.line_total:.2f} TL",
        ])

    line_table = Table(line_data, colWidths=[7.5 * cm, 3 * cm, 3.5 * cm, 3.5 * cm], repeatRows=1)
    line_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_COLOR),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "DejaVuSans-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "DejaVuSans"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#dcdde1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_BG]),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    elements.append(line_table)
    elements.append(Spacer(1, 0.5 * cm))

    # --- Toplamlar ---
    totals_data = [
        [t("Ara Toplam:"), f"{invoice.subtotal:.2f} TL"],
        [t(f"KDV (%{invoice.tax_rate:g}):"), f"{invoice.tax_amount:.2f} TL"],
        [t("GENEL TOPLAM:"), f"{invoice.total_amount:.2f} TL"],
    ]

    totals_table = Table(
        totals_data,
        colWidths=[5.8 * cm, 4.2 * cm],
    )

    totals_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 1), "DejaVuSans"),
        ("FONTNAME", (0, 2), (-1, 2), "DejaVuSans-Bold"),
        ("FONTSIZE", (0, 0), (-1, 1), 10),
        ("FONTSIZE", (0, 2), (-1, 2), 13),

        # Etiketler ve tutarlar düzgün hizalanır.
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),

        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, 1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, 1), 5),

        ("BACKGROUND", (0, 2), (-1, 2), LIGHT_BG),
        ("LINEABOVE", (0, 2), (-1, 2), 1, BRAND_COLOR),
        ("TOPPADDING", (0, 2), (-1, 2), 8),
        ("BOTTOMPADDING", (0, 2), (-1, 2), 8),
        ("TEXTCOLOR", (0, 2), (-1, 2), BRAND_COLOR),
    ]))

    # Sayfanın sağ kenarına düzgün oturur.
    totals_wrapper = Table(
        [["", totals_table]],
        colWidths=[8 * cm, 10 * cm],
    )
    totals_wrapper.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(totals_wrapper)
    elements.append(Spacer(1, 1 * cm))

    if invoice.notes:
        elements.append(Paragraph(t("Notlar:"), bold))
        elements.append(Paragraph(invoice.notes, normal))
        elements.append(Spacer(1, 0.5 * cm))

    elements.append(HRFlowable(width="100%", color=colors.HexColor("#dcdde1"), thickness=0.8))
    elements.append(Spacer(1, 0.3 * cm))
    elements.append(Paragraph(t("Bu fatura elektronik ortamda oluşturulmuştur."), small_muted))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


def invoice_pdf(request, invoice_pk):
    """Fatura PDF dosyası oluştur ve indir."""
    invoice = get_object_or_404(
        Invoice.objects.select_related("sales_order", "customer"),
        pk=invoice_pk,
    )
    pdf_bytes = build_invoice_pdf_bytes(invoice)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{invoice.invoice_number}.pdf"'
    return response


@login_required
def customer_order_list(request):
    """Müşterinin kendi siparişlerini listeler."""
    if not is_buyer(request.user):
        return redirect("portal")

    customer = getattr(request.user, "customer_profile", None)
    orders = (
        SalesOrder.objects.filter(customer=customer).prefetch_related("lines__product")
        if customer else SalesOrder.objects.none()
    )
    return render(request, "distribution/customer_order_list.html", {"orders": orders})


@login_required
def customer_order_detail(request, pk):
    """Müşterinin kendi sipariş detayını gösterir."""
    if not is_buyer(request.user):
        return redirect("portal")

    customer = getattr(request.user, "customer_profile", None)
    order = get_object_or_404(SalesOrder, pk=pk, customer=customer)
    linked_production_orders = ProductionOrder.objects.filter(
        reference_order_number=order.order_number
    )
    return render(
        request,
        "distribution/customer_order_detail.html",
        {"order": order, "linked_production_orders": linked_production_orders},
    )


@transaction.atomic()
def _process_customer_order(order, user):
    """
    Sipariş kalemlerini kontrol eder:
    - Stok yeterliyse rezerve eder.
    - Stok yetersizse eksik miktar için üretim emri açar.
    """
    fg_wh = Warehouse.objects.filter(code="DEP-MM").first()
    raw_wh = Warehouse.objects.filter(code="DEP-HM").first()
    if not fg_wh:
        raise ValueError("Mamul deposu (DEP-MM) bulunamadı.")

    fully_fulfilled_from_stock = True

    for line in order.lines.select_related("product"):
        stock, _ = Stock.objects.select_for_update().get_or_create(
            product=line.product,
            warehouse=fg_wh,
            defaults={"quantity": Decimal("0"), "reserved_quantity": Decimal("0")},
        )
        available = stock.available_quantity

        if available >= line.quantity:
            stock.reserved_quantity += line.quantity
            stock.save(update_fields=["reserved_quantity", "updated_at"])
            continue

        fully_fulfilled_from_stock = False
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
            raise ValueError(
                f"{line.product.name} için üretim yapılamıyor: "
                "aktif Reçete/Rota/Hammadde Deposu/Üretim Hattı eksik."
            )

        timestamp = timezone.now().strftime("%y%m%d%H%M%S")
        po_number = f"PO-{order.order_number}-{line.pk}-{timestamp[-4:]}"

        po = ProductionOrder.objects.create(
            order_number=po_number,
            product=line.product,
            bill_of_material=bom,
            routing=routing,
            production_line=prod_line,
            raw_materials_warehouse=raw_wh,
            finished_goods_warehouse=fg_wh,
            planned_quantity=shortfall,
            reference_order_number=order.order_number,
            created_by=user,
            planned_start_date=timezone.now(),
            planned_end_date=order.promised_delivery_date or timezone.now(),
        )
        po.create_components_from_bom()
        po.create_operations_from_routing()

    order.status = (
        SalesOrder.Status.CONFIRMED if fully_fulfilled_from_stock else SalesOrder.Status.IN_PRODUCTION
    )
    order.save(update_fields=["status", "updated_at"])


@login_required
def customer_create_order(request, variant_pk):
    """Müşteri satın alma ekranından sipariş oluşturur."""
    if not is_buyer(request.user):
        return redirect("portal")

    variant = get_object_or_404(ProductVariant, pk=variant_pk)
    customer = getattr(request.user, "customer_profile", None)

    if not customer:
        messages.error(request, "Müşteri profiliniz bulunamadı.")
        return redirect("distribution:customer_purchase_detail", variant_pk=variant_pk)

    if request.method != "POST":
        return redirect("distribution:customer_purchase_detail", variant_pk=variant_pk)

    try:
        quantity = Decimal(request.POST.get("quantity", "").strip())
    except (InvalidOperation, AttributeError):
        messages.error(request, "Geçerli bir miktar giriniz.")
        return redirect("distribution:customer_purchase_detail", variant_pk=variant_pk)

    if quantity <= 0:
        messages.error(request, "Miktar sıfırdan büyük olmalıdır.")
        return redirect("distribution:customer_purchase_detail", variant_pk=variant_pk)

    timestamp = timezone.now().strftime("%y%m%d%H%M%S")
    order_number = f"SO-{customer.code}-{timestamp}"

    with transaction.atomic():
        order = SalesOrder.objects.create(
            customer=customer,
            order_number=order_number,
            status=SalesOrder.Status.DRAFT,
        )
        SalesOrderLine.objects.create(
            sales_order=order,
            product=variant.product,
            quantity=quantity,
            unit_price=variant.price,
        )

    from .tasks import process_order_fulfillment_task
    order_pk = order.pk
    user_pk = request.user.pk
    transaction.on_commit(
        lambda language_code=get_language(): process_order_fulfillment_task.delay(order_pk, user_pk, language_code)
    )
    messages.success(request, f"{order.order_number} numaralı siparişiniz oluşturuldu.")

    return redirect("distribution:customer_order_detail", pk=order.pk)

def customer_purchase(request):
    """Müşterinin ürün seçip satın alabileceği ekran."""
    if not is_buyer(request.user):
        return redirect("portal")

    query = request.GET.get("q", "").strip()

    variants = ProductVariant.objects.select_related(
        "shoe_model", "color", "size", "product"
    ).filter(is_active=True)

    if query:
        variants = variants.filter(
            Q(sku__icontains=query)
            | Q(shoe_model__name__icontains=query)
        )

    return render(
        request,
        "distribution/customer_purchase.html",
        {"variants": variants, "query": query},
    )


def customer_purchase_detail(request, variant_pk):
    """Seçilen ürün için miktar girilip sipariş oluşturulan ekran."""
    if not is_buyer(request.user):
        return redirect("portal")

    variant = get_object_or_404(
        ProductVariant.objects.select_related("shoe_model", "color", "size", "product"),
        pk=variant_pk,
        is_active=True,
    )

    return render(
        request,
        "distribution/customer_purchase_detail.html",
        {"variant": variant},
    )
