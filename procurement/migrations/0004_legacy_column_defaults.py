from django.db import migrations


# 0002 removed these fields from Django's state but deliberately kept their
# PostgreSQL columns. New ORM inserts omit them, so NOT NULL columns need
# database defaults as well as the old Python defaults. Existing values stay intact.
LEGACY_DEFAULTS = (
    ("procurement_supplier", "payment_terms", "''"),
    ("procurement_supplier", "delivery_lead_time_days", "0"),
    ("procurement_purchaseorder", "total_amount", "0"),
    ("procurement_purchaseorderline", "tax_rate", "20"),
)


def set_defaults(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    quote = schema_editor.quote_name
    for table, column, default in LEGACY_DEFAULTS:
        schema_editor.execute(
            f"ALTER TABLE {quote(table)} ALTER COLUMN {quote(column)} SET DEFAULT {default}"
        )


def remove_defaults(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    quote = schema_editor.quote_name
    for table, column, _ in LEGACY_DEFAULTS:
        schema_editor.execute(
            f"ALTER TABLE {quote(table)} ALTER COLUMN {quote(column)} DROP DEFAULT"
        )


class Migration(migrations.Migration):
    dependencies = [("procurement", "0003_alter_purchaseorder_created_at_and_more")]
    operations = [migrations.RunPython(set_defaults, remove_defaults)]
