"""Queue isolation, resumability and concurrency of local document embeddings."""

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from celery.exceptions import Retry
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from config.celery import app
from .models import Document, DocumentChunk
from .services.embedding_pipeline import (
    chunk_generation,
    dispatch_document_embeddings,
    embed_document,
    record_embedding_error,
)
from .services.local_embeddings import (
    LocalEmbeddingConfigurationError,
    LocalEmbeddingError,
    LocalEmbeddingInputError,
    get_embedding_profile,
)
from .tasks import embed_document_chunks, process_document


STORAGE = {"default": {"BACKEND": "django.core.files.storage.InMemoryStorage"}}
UNIT_VECTOR = [1.0] + [0.0] * 383


def ready_document(count=3):
    document = Document.objects.create(
        title="Embedding pipeline fixture",
        file=SimpleUploadedFile("procedure.txt", b"Quality procedure", content_type="text/plain"),
        status=Document.Status.READY,
    )
    for index in range(count):
        content = f"Quality procedure section {index}"
        DocumentChunk.objects.create(
            document=document,
            chunk_index=index,
            content=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            token_count=5,
        )
    return document


def fake_service():
    return SimpleNamespace(
        profile=get_embedding_profile(),
        embed_passages=Mock(side_effect=lambda texts: [UNIT_VECTOR[:] for _ in texts]),
    )


class EmbeddingProfileTests(SimpleTestCase):
    def test_profile_is_independent_of_machine_path_and_batch_tuning(self):
        profile = get_embedding_profile()
        with self.settings(AI_EMBEDDING_MODEL_PATH="missing", AI_EMBEDDING_BATCH_SIZE=7, AI_EMBEDDING_THREADS=2):
            self.assertEqual(get_embedding_profile(), profile)

    def test_runtime_or_prefix_changes_invalidate_profile(self):
        profile = get_embedding_profile()
        with patch("ai.services.local_embeddings.version", return_value="changed"):
            self.assertNotEqual(get_embedding_profile().profile_hash, profile.profile_hash)
        with patch("ai.services.local_embeddings.PASSAGE_PREFIX", "different: "):
            self.assertNotEqual(get_embedding_profile().profile_hash, profile.profile_hash)

    def test_unapproved_profile_fails_before_model_loading(self):
        for overrides in (
            {"AI_EMBEDDING_DIMENSION": 768},
            {"AI_EMBEDDING_MODEL_ID": "other-model"},
            {"AI_EMBEDDING_MODEL_REVISION": "other-revision"},
            {"AI_EMBEDDING_OFFLINE": False},
        ):
            with self.subTest(overrides=overrides), self.settings(**overrides):
                with self.assertRaises(LocalEmbeddingConfigurationError):
                    get_embedding_profile()

    def test_celery_routes_separate_embedding_and_chunking_tasks(self):
        self.assertEqual(app.amqp.router.route({}, embed_document_chunks.name)["queue"].name, "embeddings")
        self.assertEqual(app.amqp.router.route({}, process_document.name)["queue"].name, "celery")


@override_settings(STORAGES=STORAGE, AI_EMBEDDING_BATCH_SIZE=2)
class EmbeddingPipelineTests(TestCase):
    def setUp(self):
        self.document = ready_document()
        self.generation = chunk_generation(self.document)
        self.service = fake_service()
        self.profile = self.service.profile
        self.loader = self.enterContext(
            patch("ai.services.embedding_pipeline.get_local_embedding_service", return_value=self.service)
        )

    def run_pipeline(self, generation=None, profile_hash=None):
        return embed_document(
            str(self.document.public_id),
            generation or self.generation,
            profile_hash or self.profile.profile_hash,
        )

    def test_batches_write_all_metadata_and_pass_unprefixed_text_to_service(self):
        result = self.run_pipeline()
        self.assertEqual(result, {"status": "embedded", "embedded_count": 3})
        self.assertEqual([len(call.args[0]) for call in self.service.embed_passages.call_args_list], [2, 1])
        sent_texts = [text for call in self.service.embed_passages.call_args_list for text in call.args[0]]
        self.assertEqual(sent_texts, list(self.document.chunks.values_list("content", flat=True)))
        for chunk in self.document.chunks.all():
            self.assertEqual(list(chunk.embedding), UNIT_VECTOR)
            self.assertEqual(chunk.embedding_model, self.profile.model_id)
            self.assertEqual(chunk.embedding_dimensions, 384)
            self.assertEqual(chunk.embedding_profile_hash, self.profile.profile_hash)
            self.assertIsNotNone(chunk.embedded_at)

    def test_duplicate_task_skips_saved_vectors_without_loading_model(self):
        self.run_pipeline()
        timestamps = list(self.document.chunks.values_list("embedded_at", flat=True))
        self.loader.reset_mock()
        self.service.embed_passages.reset_mock()
        self.assertEqual(self.run_pipeline(), {"status": "already_embedded", "embedded_count": 0})
        self.loader.assert_not_called()
        self.assertEqual(list(self.document.chunks.values_list("embedded_at", flat=True)), timestamps)

    def test_failed_batch_rolls_back_and_retry_keeps_completed_batch(self):
        self.service.embed_passages.side_effect = [[UNIT_VECTOR, UNIT_VECTOR], LocalEmbeddingError("temporary")]
        with self.assertRaises(LocalEmbeddingError):
            self.run_pipeline()
        completed = dict(self.document.chunks.filter(embedding__isnull=False).values_list("pk", "embedded_at"))
        self.assertEqual(len(completed), 2)
        self.service.embed_passages.side_effect = lambda texts: [UNIT_VECTOR[:] for _ in texts]
        self.assertEqual(self.run_pipeline(), {"status": "embedded", "embedded_count": 1})
        self.assertEqual(dict(self.document.chunks.filter(pk__in=completed).values_list("pk", "embedded_at")), completed)

    def test_wrong_queued_profile_fails_without_inference(self):
        with self.assertRaises(LocalEmbeddingConfigurationError):
            self.run_pipeline(profile_hash="f" * 64)
        self.loader.assert_not_called()
        self.assertFalse(self.document.chunks.filter(embedding__isnull=False).exists())

    def test_stale_loaded_model_profile_fails_without_inference(self):
        self.service.profile = replace(self.profile, profile_hash="f" * 64)
        with self.assertRaises(LocalEmbeddingConfigurationError):
            self.run_pipeline()
        self.service.embed_passages.assert_not_called()

    def test_mixed_stored_profile_fails_before_writing_pending_chunks(self):
        first = self.document.chunks.first()
        DocumentChunk.objects.filter(pk=first.pk).update(
            embedding=UNIT_VECTOR, embedding_model="other-model", embedding_dimensions=384,
            embedding_profile_hash="f" * 64, embedded_at=timezone.now(),
        )
        with self.assertRaises(LocalEmbeddingConfigurationError):
            self.run_pipeline()
        self.loader.assert_not_called()
        self.assertEqual(self.document.chunks.filter(embedding__isnull=True).count(), 2)

    def test_replaced_chunks_reject_old_generation_even_with_same_content(self):
        original = list(self.document.chunks.values("chunk_index", "content", "content_hash", "token_count"))
        self.document.chunks.all().delete()
        DocumentChunk.objects.bulk_create([DocumentChunk(document=self.document, **fields) for fields in original])
        self.assertNotEqual(chunk_generation(self.document), self.generation)
        self.assertEqual(self.run_pipeline()["status"], "stale")
        self.loader.assert_not_called()
        record_embedding_error(str(self.document.public_id), self.generation, "old error")
        self.document.refresh_from_db()
        self.assertEqual(self.document.processing_error, "")

    def test_non_ready_or_deleted_document_skips_model(self):
        Document.objects.filter(pk=self.document.pk).update(status=Document.Status.QUEUED)
        self.assertEqual(self.run_pipeline()["status"], "skipped")
        self.document.delete()
        self.assertEqual(self.run_pipeline()["status"], "deleted")
        self.loader.assert_not_called()

    def test_changed_content_with_unchanged_checksum_fails(self):
        self.document.chunks.update(content="modified text")
        with self.assertRaises(LocalEmbeddingInputError):
            self.run_pipeline()
        self.loader.assert_not_called()

    def test_invalid_batch_outputs_are_never_partially_saved(self):
        for vectors in (
            [UNIT_VECTOR],
            [UNIT_VECTOR, [1.0, 0.0]],
            [UNIT_VECTOR, [float("nan")] + [0.0] * 383],
            [UNIT_VECTOR, [0.0] * 384],
        ):
            with self.subTest(vectors_count=len(vectors)):
                self.service.embed_passages.side_effect = None
                self.service.embed_passages.return_value = vectors
                with self.assertRaises(LocalEmbeddingConfigurationError):
                    self.run_pipeline()
                self.assertFalse(self.document.chunks.filter(embedding__isnull=False).exists())

    def test_chunk_commit_publishes_only_to_embedding_queue_without_inference(self):
        Document.objects.filter(pk=self.document.pk).update(status=Document.Status.QUEUED)
        with patch.object(embed_document_chunks, "apply_async") as publish:
            with self.captureOnCommitCallbacks(execute=True):
                result = process_document.run(str(self.document.public_id))
                publish.assert_not_called()
            self.assertEqual(result["status"], "ready")
            publish.assert_called_once()
            self.assertEqual(publish.call_args.kwargs["queue"], "embeddings")
            self.assertEqual(publish.call_args.kwargs["args"], [
                str(self.document.public_id), chunk_generation(self.document), self.profile.profile_hash,
            ])
        self.loader.assert_not_called()

    def test_rollback_does_not_publish_embedding_task(self):
        Document.objects.filter(pk=self.document.pk).update(status=Document.Status.QUEUED)
        with patch.object(embed_document_chunks, "apply_async") as publish:
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        process_document.run(str(self.document.public_id))
                        raise RuntimeError("rollback")
                except RuntimeError:
                    pass
            publish.assert_not_called()

    def test_broker_failure_is_visible_and_chunks_remain_pending(self):
        with patch.object(embed_document_chunks, "apply_async", side_effect=OSError("broker down")):
            dispatch_document_embeddings(str(self.document.public_id), self.generation)
        self.document.refresh_from_db()
        self.assertEqual(self.document.status, Document.Status.READY)
        self.assertTrue(self.document.processing_error.startswith("Embedding: "))
        self.assertEqual(self.document.chunks.filter(embedding__isnull=True).count(), 3)
        self.run_pipeline()
        self.document.refresh_from_db()
        self.assertEqual(self.document.processing_error, "")

    def test_wrong_queue_is_rejected_before_model_loading(self):
        embed_document_chunks.push_request(called_directly=False, is_eager=False, delivery_info={"routing_key": "celery"})
        try:
            with self.assertRaises(LocalEmbeddingConfigurationError):
                embed_document_chunks.run(str(self.document.public_id), self.generation, self.profile.profile_hash)
        finally:
            embed_document_chunks.pop_request()
        self.loader.assert_not_called()

    def test_transient_failure_retries_but_stops_at_limit(self):
        args = (str(self.document.public_id), self.generation, self.profile.profile_hash)
        with patch("ai.tasks.embed_document", side_effect=LocalEmbeddingError("temporary")):
            with patch.object(embed_document_chunks, "retry", side_effect=Retry()) as retry:
                with self.assertRaises(Retry):
                    embed_document_chunks.run(*args)
                self.assertEqual(retry.call_args.kwargs["countdown"], 2)
            embed_document_chunks.push_request(retries=2, called_directly=True)
            try:
                with patch.object(embed_document_chunks, "retry") as retry:
                    with self.assertRaises(LocalEmbeddingError):
                        embed_document_chunks.run(*args)
                    retry.assert_not_called()
            finally:
                embed_document_chunks.pop_request()
        self.document.refresh_from_db()
        self.assertIn("sınırına", self.document.processing_error)

    def test_configuration_failure_is_not_retried(self):
        with patch.object(embed_document_chunks, "retry") as retry:
            with self.assertRaises(LocalEmbeddingConfigurationError):
                embed_document_chunks.run(str(self.document.public_id), self.generation, "f" * 64)
            retry.assert_not_called()
        self.loader.assert_not_called()


@override_settings(STORAGES=STORAGE)
class ConcurrentEmbeddingTests(TransactionTestCase):
    available_apps = ["django.contrib.auth", "django.contrib.contenttypes", "ai"]

    def test_duplicate_tasks_serialize_without_duplicate_inference(self):
        document = ready_document(count=1)
        generation = chunk_generation(document)
        service = fake_service()
        started = threading.Event()
        release = threading.Event()
        second_started = threading.Event()

        def blocking_embed(texts):
            started.set()
            if not release.wait(timeout=10):
                raise RuntimeError("Concurrent test timed out")
            return [UNIT_VECTOR[:] for _ in texts]

        service.embed_passages.side_effect = blocking_embed

        def run(second=False):
            close_old_connections()
            try:
                if second:
                    second_started.set()
                return embed_document(str(document.public_id), generation, service.profile.profile_hash)
            finally:
                close_old_connections()

        with patch("ai.services.embedding_pipeline.get_local_embedding_service", return_value=service):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(run)
                try:
                    self.assertTrue(started.wait(timeout=10))
                    second = pool.submit(run, True)
                    self.assertTrue(second_started.wait(timeout=10))
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=10)["status"], "embedded")
                self.assertEqual(second.result(timeout=10)["status"], "already_embedded")
        service.embed_passages.assert_called_once()
        self.assertEqual(document.chunks.filter(embedding__isnull=False).count(), 1)
