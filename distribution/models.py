from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models

from django.conf import settings

import uuid
from django.utils import timezone

class Customer(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="customer_profile",
        null=True,          # Migration: for existing customers
        blank=True,         # Can be left blank in the admin panel
        verbose_name="Kullanıcı Hesabı",
    )
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=150)
    tax_number = models.CharField(max_length=50, blank=True)
    email = models.EmailField()  # blank=True removed, now required
    phone = models.CharField(max_length=30, blank=True)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Müşteri"
        verbose_name_plural = "Müşteriler"

    def __str__(self):
        return f"{self.code} - {self.name}"


class SalesOrder(models.Model):
    """Müşteri siparişleri."""


    class Status(models.TextChoices):
        DRAFT = "draft", "Taslak"
        CONFIRMED = "confirmed", "Onaylandı"
        IN_PRODUCTION = "in_production", "Üretimde"
        READY_TO_SHIP = "ready_to_ship", "Sevkiyata Hazır"
        SHIPPED = "shipped", "Sevk Edildi"
        COMPLETED = "completed", "Tamamlandı"
        CANCELLED = "cancelled", "İptal Edildi"

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Satış Siparişi"
        verbose_name_plural = "Satış Siparişleri"

    order_number = models.CharField(
        max_length=50,
        unique=True,
        blank=True,
        verbose_name="Sipariş Numarası",
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name="sales_orders",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    requested_delivery_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Talep Edilen Teslim Tarihi",
    )
    promised_delivery_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Taahhüt Edilen Teslim Tarihi",
    )
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def total_amount(self):
        return sum(
            line.line_total for line in self.lines.all()
        ) if self.lines.exists() else Decimal("0")

    def __str__(self):
        return f"{self.order_number} - {self.customer}"

    def save(self, *args, **kwargs):
        """Sipariş numarasını otomatik üret."""
        if not self.order_number:
            now_str = timezone.now().strftime("%Y%m%d")
            unique_suffix = uuid.uuid4().hex[:6].upper()
            cust_code = self.customer.code if self.customer else "GEN"
            self.order_number = f"SO-{now_str}-{cust_code}-{unique_suffix}"
        super().save(*args, **kwargs)



class SalesOrderLine(models.Model):
    """Sipariş kalemleri."""

    class Meta:
        ordering = ["id"]
        verbose_name = "Sipariş Kalemi"
        verbose_name_plural = "Sipariş Kalemleri"

    sales_order = models.ForeignKey(
        SalesOrder,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    product = models.ForeignKey(
        "inventory.Product",
        on_delete=models.PROTECT,
        related_name="sales_order_lines",
        limit_choices_to={"product_type": "finished_good"},
    )
    quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    unit_price = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )
    line_total = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
    )
    produced_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=Decimal("0"),
    )
    shipped_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=Decimal("0"),
    )

    def save(self, *args, **kwargs):
        self.line_total = (self.quantity or Decimal("0")) * (self.unit_price or Decimal("0"))
        super().save(*args, **kwargs)
        
    def __str__(self):
        return f"{self.product} - {self.quantity}"


class Invoice(models.Model):
    """Satış siparişine bağlı fatura kaydı."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Taslak"
        ISSUED = "issued", "Düzenlendi"
        PAID = "paid", "Ödendi"
        CANCELLED = "cancelled", "İptal"

    invoice_number = models.CharField(
        max_length=50,
        unique=True,
        verbose_name="Fatura Numarası",
    )
    sales_order = models.OneToOneField(
        SalesOrder,
        on_delete=models.CASCADE,
        related_name="invoice",
        verbose_name="Satış Siparişi",
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        verbose_name="Müşteri",
    )
    issue_date = models.DateField(
        verbose_name="Fatura Tarihi",
    )
    due_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Vade Tarihi",
    )
    subtotal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name="Ara Toplam",
    )
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=20,
        verbose_name="KDV Oranı (%)",
    )
    tax_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name="KDV Tutarı",
    )
    total_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name="Genel Toplam",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        verbose_name="Durum",
    )
    notes = models.TextField(
        blank=True,
        verbose_name="Notlar",
    )
    emailed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Fatura E-posta Gönderim Tarihi",
        help_text="Fatura müşteriye e-posta ile gönderildiğinde doldurulur.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Fatura"
        verbose_name_plural = "Faturalar"
        ordering = ["-issue_date", "-invoice_number"]

    def __str__(self):
        return f"{self.invoice_number} - {self.customer}"

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            from django.utils import timezone
            year = timezone.now().year
            last = Invoice.objects.filter(
                invoice_number__startswith=f"INV-{year}-"
            ).order_by("-invoice_number").first()
            if last:
                last_num = int(last.invoice_number.split("-")[-1])
                new_num = last_num + 1
            else:
                new_num = 1
            self.invoice_number = f"INV-{year}-{new_num:04d}"
        super().save(*args, **kwargs)
