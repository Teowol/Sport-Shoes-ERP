"""Celery tasks for private AI document processing."""

import logging
from functools import partial

from celery import shared_task
from django.db import OperationalError, transaction

from .models import Document, DocumentChunk
from .services.document_processing import build_document_chunks
from .services.embedding_pipeline import (
    EMBEDDINGS_QUEUE,
    chunk_generation,
    dispatch_document_embeddings,
    embed_document,
    record_embedding_error,
)
from .services.local_embeddings import (
    LocalEmbeddingConfigurationError,
    LocalEmbeddingError,
    LocalEmbeddingInputError,
)


logger = logging.getLogger(__name__)
MAX_PROCESSING_RETRIES = 2


def _admin_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]


@shared_task(bind=True, max_retries=MAX_PROCESSING_RETRIES)
def process_document(self, public_id: str):
    """Claim and replace a document's chunks without exposing its database PK."""
    try:
        claimed = Document.objects.filter(
            public_id=public_id,
            status=Document.Status.QUEUED,
        ).update(
            status=Document.Status.PROCESSING,
            processing_error="",
        )
    except OperationalError as exc:
        logger.exception("Document processing could not claim %s", public_id)
        raise self.retry(
            exc=exc,
            countdown=2 ** (self.request.retries + 1),
            max_retries=MAX_PROCESSING_RETRIES,
        )
    if not claimed:
        return {"status": "skipped"}

    try:
        document = Document.objects.get(public_id=public_id)
        chunk_data = build_document_chunks(document)
        with transaction.atomic():
            locked_document = Document.objects.select_for_update().get(public_id=public_id)
            if locked_document.status != Document.Status.PROCESSING:
                return {"status": "skipped"}
            locked_document.chunks.all().delete()
            DocumentChunk.objects.bulk_create(
                [
                    DocumentChunk(
                        document=locked_document,
                        chunk_index=chunk.chunk_index,
                        content=chunk.content,
                        token_count=chunk.token_count,
                        page_number=chunk.page_number,
                        section_title=chunk.section_title,
                        content_hash=chunk.content_hash,
                    )
                    for chunk in chunk_data
                ]
            )
            locked_document.status = Document.Status.READY
            locked_document.processing_error = ""
            locked_document.save(update_fields=("status", "processing_error", "updated_at"))
            generation = chunk_generation(locked_document)
            transaction.on_commit(
                partial(dispatch_document_embeddings, str(locked_document.public_id), generation)
            )
        return {"status": "ready", "chunk_count": len(chunk_data)}
    except Document.DoesNotExist:
        return {"status": "deleted"}
    except (OSError, OperationalError) as exc:
        logger.exception("Temporary document processing failure for %s", public_id)
        if self.request.retries >= MAX_PROCESSING_RETRIES:
            Document.objects.filter(
                public_id=public_id,
                status=Document.Status.PROCESSING,
            ).update(
                status=Document.Status.FAILED,
                processing_error=_admin_error(exc),
            )
            return {"status": "failed"}
        Document.objects.filter(
            public_id=public_id,
            status=Document.Status.PROCESSING,
        ).update(
            status=Document.Status.QUEUED,
            processing_error=_admin_error(exc),
        )
        raise self.retry(
            exc=exc,
            countdown=2 ** (self.request.retries + 1),
            max_retries=MAX_PROCESSING_RETRIES,
        )
    except Exception as exc:
        logger.exception("Document processing failed for %s", public_id)
        Document.objects.filter(
            public_id=public_id,
            status=Document.Status.PROCESSING,
        ).update(
            status=Document.Status.FAILED,
            processing_error=_admin_error(exc),
        )
        return {"status": "failed"}


@shared_task(
    bind=True,
    queue=EMBEDDINGS_QUEUE,
    max_retries=2,
    acks_late=True,
    reject_on_worker_lost=True,
)
def embed_document_chunks(self, public_id: str, generation: str, expected_profile_hash: str):
    """Run local inference only on the embeddings queue, with bounded retries."""
    try:
        if (
            not self.request.called_directly
            and not self.request.is_eager
            and (self.request.delivery_info or {}).get("routing_key") != EMBEDDINGS_QUEUE
        ):
            raise LocalEmbeddingConfigurationError("Embedding task delivered to the wrong queue.")
        return embed_document(public_id, generation, expected_profile_hash)
    except (LocalEmbeddingConfigurationError, LocalEmbeddingInputError):
        _record_embedding_task_error(public_id, generation, "Profil, model veya içerik doğrulaması başarısız.")
        raise
    except (LocalEmbeddingError, OperationalError, OSError) as exc:
        _record_embedding_task_error(public_id, generation, "İşleme tamamlanamadı; sınırlı yeniden deneme.")
        if self.request.retries >= self.max_retries:
            _record_embedding_task_error(public_id, generation, "Yeniden deneme sınırına ulaşıldı.")
            raise
        raise self.retry(exc=exc, countdown=2 ** (self.request.retries + 1))
    except Exception:
        _record_embedding_task_error(public_id, generation, "Beklenmeyen işleme hatası.")
        raise


def _record_embedding_task_error(public_id, generation, message):
    try:
        record_embedding_error(public_id, generation, message)
    except Exception:
        logger.exception("Could not record embedding task failure for %s", public_id)
