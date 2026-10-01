from importlib import import_module
from unittest import skipUnless

from django.db import connection
from django.test import TestCase

from procurement.models import Supplier


@skipUnless(connection.vendor == "postgresql", "Legacy columns are retained on PostgreSQL")
class LegacyColumnDefaultsTests(TestCase):
    def test_default_migration_preserves_archived_supplier_values(self):
        supplier = Supplier.objects.create(code="LEGACY", company_name="Existing supplier")
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE procurement_supplier SET payment_terms = %s, delivery_lead_time_days = %s WHERE id = %s",
                ["60 days", 14, supplier.pk],
            )
        migration = import_module("procurement.migrations.0004_legacy_column_defaults")
        with connection.schema_editor() as editor:
            migration.set_defaults(None, editor)
        Supplier.objects.create(code="NEW", company_name="New supplier")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT code, payment_terms, delivery_lead_time_days FROM procurement_supplier ORDER BY code"
            )
            self.assertEqual(cursor.fetchall(), [("LEGACY", "60 days", 14), ("NEW", "", 0)])
