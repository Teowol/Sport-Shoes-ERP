"""Backfill selection, safe publication, interruption and resumability."""

from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from ai.management.commands.embed_pending_chunks import Command
from ai.models import Document, DocumentChunk
from ai.services.embedding_pipeline import chunk_generation, embed_document
from ai.services.local_embeddings import get_embedding_profile
from ai.tasks import embed_document_chunks
from ai.test_embedding_pipeline import STORAGE, UNIT_VECTOR, fake_service, ready_document


@override_settings(STORAGES=STORAGE, AI_EMBEDDING_BATCH_SIZE=2)
class EmbedPendingChunksTests(TestCase):
    def setUp(self):
        self.output = StringIO()
        self.profile = get_embedding_profile()
        self.publish = self.enterContext(patch.object(embed_document_chunks, "apply_async"))
        self.loader = self.enterContext(patch(
            "ai.services.embedding_pipeline.get_local_embedding_service",
            side_effect=AssertionError("The command must not load the model"),
        ))

    def run_command(self, **options):
        self.output = StringIO()
        call_command("embed_pending_chunks", stdout=self.output, **options)
        return self.output.getvalue()

    def mark_embedded(self, chunks, **overrides):
        values = dict(
            embedding=UNIT_VECTOR,
            embedding_model=self.profile.model_id,
            embedding_dimensions=self.profile.dimensions,
            embedding_profile_hash=self.profile.profile_hash,
            embedded_at=timezone.now(),
        )
        values.update(overrides)
        chunks.update(**values)

    def simulate_worker(self, **kwargs):
        service = fake_service()
        with patch("ai.services.embedding_pipeline.get_local_embedding_service", return_value=service):
            result = embed_document(*kwargs["args"])
        return result

    def test_only_ready_documents_with_pending_chunks_are_published_once(self):
        pending = ready_document(count=3)
        completed = ready_document(count=1)
        self.mark_embedded(completed.chunks.all())
        ready_document(count=0)
        for status in (Document.Status.QUEUED, Document.Status.UPLOADED, Document.Status.PROCESSING, Document.Status.FAILED):
            document = ready_document(count=1)
            Document.objects.filter(pk=document.pk).update(status=status)

        output = self.run_command(batch_size=1)
        self.publish.assert_called_once_with(
            args=[str(pending.public_id), chunk_generation(pending), self.profile.profile_hash],
            queue="embeddings",
            retry=True,
            retry_policy={"max_retries": 2, "interval_start": 0, "interval_step": 0.2, "interval_max": 0.5},
        )
        self.assertIn("doküman=1, bekleyen chunk=3", output)
        self.loader.assert_not_called()
        self.assertEqual(pending.chunks.filter(embedding__isnull=True).count(), 3)

    def test_dry_run_respects_limit_and_does_not_publish_or_modify_records(self):
        first = ready_document(count=3)
        ready_document(count=2)
        Document.objects.filter(pk=first.pk).update(processing_error="Embedding: previous error")
        before = list(DocumentChunk.objects.order_by("pk").values())
        output = self.run_command(dry_run=True, limit=1, batch_size=1)
        self.assertIn("Önizleme: doküman=1, bekleyen chunk=3", output)
        self.assertEqual(list(DocumentChunk.objects.order_by("pk").values()), before)
        first.refresh_from_db()
        self.assertEqual(first.processing_error, "Embedding: previous error")
        self.publish.assert_not_called()
        self.loader.assert_not_called()

    def test_empty_database_is_a_successful_noop(self):
        self.assertIn("doküman=0, bekleyen chunk=0", self.run_command())
        self.publish.assert_not_called()
        self.loader.assert_not_called()

    def test_nonpositive_batch_size_and_limit_are_rejected(self):
        for options in ({"batch_size": 0}, {"batch_size": -1}, {"limit": 0}, {"limit": -1}):
            with self.subTest(options=options), self.assertRaises(CommandError):
                self.run_command(**options)
        self.publish.assert_not_called()

    def test_limit_then_resume_skips_completed_documents(self):
        documents = [ready_document(count=1) for _ in range(3)]
        self.publish.side_effect = self.simulate_worker
        self.assertIn("doküman=2", self.run_command(limit=2, batch_size=1))
        self.assertEqual(DocumentChunk.objects.filter(embedding__isnull=True).count(), 1)
        self.assertIn("doküman=1", self.run_command(batch_size=1))
        self.assertEqual(self.publish.call_count, 3)
        self.assertEqual([c.kwargs["args"][0] for c in self.publish.call_args_list], [str(d.public_id) for d in documents])
        self.assertIn("doküman=0", self.run_command())
        self.assertEqual(self.publish.call_count, 3)

    def test_keyset_scan_handles_shrinking_pending_set_and_excludes_new_documents(self):
        original = [ready_document(count=1) for _ in range(5)]
        created = []

        def publish(**kwargs):
            self.simulate_worker(**kwargs)
            if not created:
                created.append(ready_document(count=1))

        self.publish.side_effect = publish
        self.assertIn("doküman=5", self.run_command(batch_size=2))
        self.assertEqual([c.kwargs["args"][0] for c in self.publish.call_args_list], [str(d.public_id) for d in original])
        self.assertEqual(created[0].chunks.filter(embedding__isnull=True).count(), 1)

    def test_partially_embedded_document_keeps_saved_vector_and_timestamp(self):
        document = ready_document(count=3)
        saved = document.chunks.order_by("chunk_index").first()
        self.mark_embedded(document.chunks.filter(pk=saved.pk))
        saved.refresh_from_db()
        service = fake_service()

        def publish(**kwargs):
            with patch("ai.services.embedding_pipeline.get_local_embedding_service", return_value=service):
                return embed_document(*kwargs["args"])

        self.publish.side_effect = publish
        self.assertIn("bekleyen chunk=2", self.run_command())
        service.embed_passages.assert_called_once_with([c.content for c in document.chunks.exclude(pk=saved.pk)])
        self.assertEqual(document.chunks.get(pk=saved.pk).embedded_at, saved.embedded_at)
        self.assertEqual(list(document.chunks.get(pk=saved.pk).embedding), list(saved.embedding))
        self.assertIn("doküman=0", self.run_command())
        self.assertEqual(service.embed_passages.call_count, 1)

    def test_interrupt_stops_publication_and_can_resume(self):
        for _ in range(3):
            ready_document(count=1)
        calls = 0

        def publish(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return self.simulate_worker(**kwargs)

        self.publish.side_effect = publish
        with self.assertRaises(CommandError) as error:
            self.run_command(batch_size=1)
        self.assertEqual(error.exception.returncode, 130)
        self.assertIn("doküman=1", self.output.getvalue())
        self.assertEqual(DocumentChunk.objects.filter(embedding__isnull=True).count(), 2)
        self.publish.side_effect = self.simulate_worker
        self.assertIn("doküman=2", self.run_command())
        self.assertFalse(DocumentChunk.objects.filter(embedding__isnull=True).exists())

    def test_broker_failure_stops_without_marking_chunks_complete(self):
        ready_document(count=1)
        ready_document(count=1)
        self.publish.side_effect = OSError("broker unavailable")
        with self.assertRaisesMessage(CommandError, "Backfill durdu (OSError)"):
            self.run_command()
        self.publish.assert_called_once()
        self.assertIn("doküman=0", self.output.getvalue())
        self.assertEqual(DocumentChunk.objects.filter(embedding__isnull=True).count(), 2)
        self.publish.side_effect = self.simulate_worker
        self.assertIn("doküman=2", self.run_command())

    def test_stored_profile_mismatch_fails_closed_even_in_dry_run(self):
        document = ready_document(count=2)
        self.mark_embedded(document.chunks.filter(chunk_index=0), embedding_profile_hash="f" * 64)
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), self.assertRaisesMessage(CommandError, "kayıtlı embedding profili uyuşmuyor"):
                self.run_command(dry_run=dry_run)
        self.assertEqual(document.chunks.get(chunk_index=0).embedding_profile_hash, "f" * 64)
        self.assertEqual(document.chunks.filter(embedding__isnull=True).count(), 1)
        self.publish.assert_not_called()
        self.loader.assert_not_called()

    def test_invalid_runtime_profile_fails_before_publication(self):
        ready_document(count=1)
        with self.settings(AI_EMBEDDING_DIMENSION=768), self.assertRaises(CommandError):
            self.run_command()
        self.publish.assert_not_called()
        self.loader.assert_not_called()

    def test_eager_execution_is_rejected_to_keep_inference_out_of_command(self):
        ready_document(count=1)
        with patch.object(embed_document_chunks, "app", SimpleNamespace(conf=SimpleNamespace(task_always_eager=True))):
            with self.assertRaisesMessage(CommandError, "separate worker"):
                self.run_command()
        self.publish.assert_not_called()
        self.loader.assert_not_called()

    def test_document_deleted_or_reprocessed_during_scan_is_skipped(self):
        first, second, third = [ready_document(count=1) for _ in range(3)]
        prepare = Command._prepare_document

        def changed(pk, profile):
            if pk == first.pk:
                Document.objects.filter(pk=pk).delete()
            if pk == second.pk:
                Document.objects.filter(pk=pk).update(status=Document.Status.PROCESSING)
            return prepare(pk, profile)

        with patch.object(Command, "_prepare_document", side_effect=changed):
            output = self.run_command(batch_size=3, limit=1)
        self.publish.assert_called_once()
        self.assertEqual(self.publish.call_args.kwargs["args"][0], str(third.public_id))
        self.assertIn("tamamlanan/değişen=2", output)

    def test_rerun_before_worker_completion_does_not_duplicate_inference(self):
        ready_document(count=1)
        self.run_command()
        self.run_command()
        messages = [c.kwargs for c in self.publish.call_args_list]
        self.assertEqual(len(messages), 2)
        self.assertEqual(self.simulate_worker(**messages[0])["embedded_count"], 1)
        # A redelivered job must not even ask to load the model.
        self.assertEqual(embed_document(*messages[1]["args"]), {"status": "already_embedded", "embedded_count": 0})
        self.loader.assert_not_called()
