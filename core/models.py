from django.db import models

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
