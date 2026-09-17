"""Exercise the installed extension on the migrated PostgreSQL test database."""

import json

from django.db import DataError, connection, transaction
from django.test import TestCase


class PgvectorDatabaseTests(TestCase):
    def test_vector_extension_is_enabled_by_migrations(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(row[0])

    def test_384_dimension_vectors_round_trip_and_cosine_distance(self):
        first = json.dumps([1.0] + [0.0] * 383)
        orthogonal = json.dumps([0.0, 1.0] + [0.0] * 382)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT vector_dims(%s::vector(384)), "
                "%s::vector(384)::text, "
                "%s::vector(384) <=> %s::vector(384), "
                "%s::vector(384) <=> %s::vector(384)",
                [first, first, first, first, first, orthogonal],
            )
            dimensions, restored, same_distance, other_distance = cursor.fetchone()
        self.assertEqual(dimensions, 384)
        self.assertEqual(json.loads(restored), json.loads(first))
        self.assertAlmostEqual(same_distance, 0.0)
        self.assertAlmostEqual(other_distance, 1.0)

    def test_database_rejects_incorrect_vector_dimensions(self):
        with self.assertRaises(DataError):
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SELECT %s::vector(384)", ["[1, 2, 3]"])
