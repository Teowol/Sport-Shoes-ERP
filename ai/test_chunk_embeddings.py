"""Database guarantees and legacy-data compatibility for embedding storage."""

import hashlib

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DataError, IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from pgvector.django import CosineDistance

from .models import Document, DocumentChunk


@override_settings(
    STORAGES={"default": {"BACKEND": "django.core.files.storage.InMemoryStorage"}}
)
class ChunkEmbeddingStorageTests(TestCase):
    def setUp(self):
        self.document = Document.objects.create(
            title="Embedding storage fixture",
            file=SimpleUploadedFile("guide.txt", b"Quality procedure", content_type="text/plain"),
            status=Document.Status.READY,
        )
        self.chunk = DocumentChunk.objects.create(
            document=self.document,
            chunk_index=0,
            content="Quality procedure",
            content_hash=hashlib.sha256(b"Quality procedure").hexdigest(),
            token_count=2,
        )

    def embedding_values(self):
        return {
            "embedding": [1.0] + [0.0] * 383,
            "embedding_model": "intfloat/multilingual-e5-small",
            "embedding_dimensions": 384,
            "embedding_profile_hash": hashlib.sha256(b"test-profile").hexdigest(),
            "embedded_at": timezone.now(),
        }

    def test_pending_chunk_has_no_vector_or_claimed_embedding_metadata(self):
        self.chunk.refresh_from_db()
        self.assertIsNone(self.chunk.embedding)
        self.assertEqual(self.chunk.embedding_model, "")
        self.assertIsNone(self.chunk.embedding_dimensions)
        self.assertEqual(self.chunk.embedding_profile_hash, "")
        self.assertIsNone(self.chunk.embedded_at)

    def test_vector_and_metadata_round_trip_with_orm_cosine_distance(self):
        values = self.embedding_values()
        DocumentChunk.objects.filter(pk=self.chunk.pk).update(**values)
        stored = DocumentChunk.objects.annotate(
            distance=CosineDistance("embedding", values["embedding"])
        ).get(pk=self.chunk.pk)
        self.assertEqual(list(stored.embedding), values["embedding"])
        for field in ("embedding_model", "embedding_dimensions", "embedding_profile_hash", "embedded_at"):
            self.assertEqual(getattr(stored, field), values[field])
        self.assertAlmostEqual(stored.distance, 0.0)
        self.assertEqual(stored.content, self.chunk.content)
        self.assertEqual(stored.content_hash, self.chunk.content_hash)

    def test_database_rejects_partial_or_inconsistent_metadata(self):
        cases = [
            {"embedding": None},
            {"embedding_model": ""},
            {"embedding_dimensions": None},
            {"embedding_dimensions": 768},
            {"embedding_profile_hash": ""},
            {"embedding_profile_hash": "f" * 63},
            {"embedding_profile_hash": "z" * 64},
            {"embedded_at": None},
        ]
        for invalid_values in cases:
            with self.subTest(invalid_values=invalid_values):
                values = self.embedding_values() | invalid_values
                with self.assertRaises(IntegrityError) as error:
                    with transaction.atomic():
                        DocumentChunk.objects.filter(pk=self.chunk.pk).update(**values)
                self.assertEqual(
                    error.exception.__cause__.diag.constraint_name,
                    "ai_chunk_embedding_complete",
                )
        self.chunk.refresh_from_db()
        self.assertIsNone(self.chunk.embedding)

    def test_database_rejects_vector_without_metadata(self):
        with self.assertRaises(IntegrityError) as error:
            with transaction.atomic():
                DocumentChunk.objects.filter(pk=self.chunk.pk).update(
                    embedding=self.embedding_values()["embedding"]
                )
        self.assertEqual(error.exception.__cause__.diag.constraint_name, "ai_chunk_embedding_complete")

    def test_database_rejects_wrong_vector_length_even_with_valid_metadata(self):
        values = self.embedding_values() | {"embedding": [1.0, 0.0, 0.0]}
        with self.assertRaises(DataError):
            with transaction.atomic():
                DocumentChunk.objects.filter(pk=self.chunk.pk).update(**values)
        self.chunk.refresh_from_db()
        self.assertIsNone(self.chunk.embedding)


class ChunkEmbeddingMigrationTests(TransactionTestCase):
    # Scope cleanup like the existing AI transaction tests: procurement keeps
    # legacy tables outside Django's current model state.
    available_apps = ["django.contrib.auth", "django.contrib.contenttypes", "ai"]
    migrate_from = [("ai", "0003_enable_vector")]
    migrate_to = [("ai", "0004_documentchunk_embeddings")]

    def test_migration_preserves_existing_document_and_chunk(self):
        # Always restore the current schema before TransactionTestCase flushes.
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(self.migrate_from)
            old_apps = executor.loader.project_state(self.migrate_from).apps
            old_document = old_apps.get_model("ai", "Document")
            old_chunk = old_apps.get_model("ai", "DocumentChunk")
            document = old_document.objects.create(
                title="Existing procedure",
                file="ai/documents/migration-fixture.txt",
                original_filename="migration-fixture.txt",
                mime_type="text/plain",
                file_size=18,
                checksum_sha256=hashlib.sha256(b"Existing procedure").hexdigest(),
                status="ready",
            )
            chunk = old_chunk.objects.create(
                document=document,
                chunk_index=0,
                content="Existing procedure",
                token_count=2,
                page_number=3,
                section_title="Quality",
                content_hash=hashlib.sha256(b"Existing procedure").hexdigest(),
            )
            original_document = old_document.objects.values().get(pk=document.pk)
            original_chunk = old_chunk.objects.values().get(pk=chunk.pk)

            executor = MigrationExecutor(connection)
            executor.migrate(self.migrate_to)
            new_apps = executor.loader.project_state(self.migrate_to).apps
            new_document = new_apps.get_model("ai", "Document")
            new_chunk = new_apps.get_model("ai", "DocumentChunk")
            self.assertEqual(new_document.objects.values().get(pk=document.pk), original_document)
            self.assertEqual(
                new_chunk.objects.values(*original_chunk).get(pk=chunk.pk), original_chunk
            )
            self.assertEqual(
                new_chunk.objects.values(
                    "embedding", "embedding_model", "embedding_dimensions",
                    "embedding_profile_hash", "embedded_at",
                ).get(pk=chunk.pk),
                {"embedding": None, "embedding_model": "", "embedding_dimensions": None,
                 "embedding_profile_hash": "", "embedded_at": None},
            )
        finally:
            MigrationExecutor(connection).migrate(self.migrate_to)
