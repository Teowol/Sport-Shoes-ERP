from django.db import migrations

DEFAULT_MODULES = [
    ("Yapay Zekâ Asistanı", "ai:chat_page", "✦", 10),
    ("Stok Yönetimi", "inventory:stock_level_list", "▣", 20),
    ("Stok Hareketleri", "inventory:stock_movement_list", "≡", 30),
    ("Lot Takibi", "inventory:lot_tracking_list", "⎇", 40),
    ("Fire Takibi", "inventory:fire_tracking_list", "✖", 50),
    ("Üretim", "production:order_list", "⚙", 60),
    ("Üretim Operasyonları", "production:operation_list", "⛭", 70),
    ("Maliyet Takibi", "production:cost_list", "₺", 80),
    ("Kalite Kontrol", "quality:quality_check_list", "✓", 90),
    ("Dağıtım ve Satış", "distribution:sales_order_list", "↗", 100),
    ("Tedarik ve Satın Alma", "procurement:purchase_request_list", "⇄", 110),
    ("Çalışanlar", "employee_list", "☰", 120),
]


def seed_defaults(apps, schema_editor):
    """Insert the previously hard-coded portal modules once.

    Only runs when the table is empty, so existing admin-managed rows
    (e.g. restored from a backup) are never overwritten or duplicated.
    """
    ModuleLink = apps.get_model("core", "ModuleLink")
    if ModuleLink.objects.exists():
        return
    ModuleLink.objects.bulk_create(
        ModuleLink(label=label, route=route, icon=icon, sort_order=sort_order)
        for label, route, icon, sort_order in DEFAULT_MODULES
    )


def unseed_defaults(apps, schema_editor):
    """Remove exactly the rows this migration inserted (idempotent rollback)."""
    ModuleLink = apps.get_model("core", "ModuleLink")
    for label, route, _icon, sort_order in DEFAULT_MODULES:
        ModuleLink.objects.filter(
            label=label, route=route, sort_order=sort_order
        ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_module_link_modulelink"),
    ]

    operations = [
        migrations.RunPython(seed_defaults, unseed_defaults),
    ]
