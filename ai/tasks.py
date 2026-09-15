"""Celery tasks for private AI document processing."""

import logging

from celery import shared_task
from django.db import OperationalError, transaction

from .models import Document, DocumentChunk
from .services.document_processing import build_document_chunks


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
