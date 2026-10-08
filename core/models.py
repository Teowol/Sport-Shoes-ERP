from django.db import models


class ModuleLink(models.Model):
    """A card/link shown on the factory portal and in the module drawer.

    Rows are managed from the Django admin, so new modules can be added
    (or existing ones reordered/hidden) without a code change. `route`
    holds a Django URL name; entries without a valid route are skipped
    at render time instead of raising NoReverseMatch.
    """

    label = models.CharField("Etiket", max_length=120)
    route = models.CharField(
        "URL adı (route)",
        max_length=120,
        help_text="Django URL adı, örn. 'production:order_list'.",
    )
    icon = models.CharField(
        "İkon",
        max_length=8,
        blank=True,
        default="▣",
        help_text="Kart üzerinde gösterilecek tek karakterlik simge.",
    )
    group = models.CharField(
        "Grup",
        max_length=120,
        blank=True,
        default="ERP Modülleri",
        help_text="Panelde/menüde hangi başlık altında gösterilecek.",
    )
    is_active = models.BooleanField("Aktif", default=True)
    sort_order = models.PositiveIntegerField(
        "Sıra",
        default=100,
        help_text="Küçük değerler önce gösterilir.",
    )

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "Modül Bağlantısı"
        verbose_name_plural = "Modül Bağlantıları"

    def __str__(self):
        return self.label


class Employee(models.Model):
    code = models.CharField("Çalışan Kodu", max_length=30, unique=True)
    full_name = models.CharField("Ad Soyad", max_length=150)
    profession = models.CharField("Meslek", max_length=120)
    department = models.CharField("Departman", max_length=120, blank=True)
    is_active = models.BooleanField("Aktif", default=True)
    notes = models.TextField("Notlar", blank=True)

    class Meta:
        ordering = ["full_name", "code"]
        verbose_name = "Çalışan"
        verbose_name_plural = "Çalışanlar"

    def __str__(self):
        return f"{self.code} - {self.full_name}"
