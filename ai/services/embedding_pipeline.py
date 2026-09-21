"""Idempotent document embedding batches with profile and generation guards."""

import hashlib
import logging
import math

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ai.models import Document, DocumentChunk
from .local_embeddings import (
    LocalEmbeddingConfigurationError,
    LocalEmbeddingInputError,
    NORM_ABS_TOLERANCE,
    get_embedding_profile,
    get_local_embedding_service,
)


logger = logging.getLogger(__name__)
EMBEDDINGS_QUEUE = "embeddings"
ERROR_PREFIX = "Embedding: "


def chunk_generation(document) -> str:
    """Detect replacement even when reprocessing produces identical text."""
    digest = hashlib.sha256()
    for pk, index, content_hash in document.chunks.order_by("chunk_index").values_list(
        "pk", "chunk_index", "content_hash"
    ).iterator():
        digest.update(f"{pk}:{index}:{content_hash}\n".encode("ascii"))
    return digest.hexdigest()


def record_embedding_error(public_id, generation, message):
    """Never attach an old task's error to a replacement document generation."""
    with transaction.atomic():
        document = Document.objects.select_for_update().filter(public_id=public_id).first()
        if (
            document is not None
            and document.status == Document.Status.READY
            and chunk_generation(document) == generation
        ):
            Document.objects.filter(pk=document.pk).update(
                processing_error=(ERROR_PREFIX + message)[:2000]
            )


def dispatch_document_embeddings(public_id, generation):
    """Called after chunk commit. Publication failure leaves resumable chunks."""
    from ai.tasks import embed_document_chunks

    try:
        profile = get_embedding_profile()
        embed_document_chunks.apply_async(
            args=[str(public_id), generation, profile.profile_hash],
            queue=EMBEDDINGS_QUEUE,
            retry=True,
            retry_policy={"max_retries": 2, "interval_start": 0, "interval_step": 0.2, "interval_max": 0.5},
        )
    except Exception:
        logger.exception("Embedding task publication failed for %s", public_id)
        # A failing callback must not incorrectly report committed chunking as
        # failed. Keep READY as the text-processing state and expose the error.
        try:
            record_embedding_error(public_id, generation, "Kuyruğa gönderilemedi; yeniden işleme gerekli.")
        except Exception:
            logger.exception("Could not record embedding publication failure for %s", public_id)


def _validate_vectors(vectors, count, dimensions):
    if len(vectors) != count:
        raise LocalEmbeddingConfigurationError("Embedding output count mismatch.")
    for vector in vectors:
        if len(vector) != dimensions or not all(math.isfinite(value) for value in vector):
            raise LocalEmbeddingConfigurationError("Invalid embedding output.")
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isclose(norm, 1.0, abs_tol=NORM_ABS_TOLERANCE):
            raise LocalEmbeddingConfigurationError("Embedding output is not normalized.")


def embed_document(public_id, generation, expected_profile_hash):
    profile = get_embedding_profile()
    if profile.profile_hash != expected_profile_hash:
        raise LocalEmbeddingConfigurationError("Queued embedding profile mismatch.")
    batch_size = settings.AI_EMBEDDING_BATCH_SIZE
    if batch_size < 1:
        raise LocalEmbeddingConfigurationError("Invalid embedding batch size.")
    embedded_count = 0

    while True:
        # The document lock is shared with chunk replacement. Each batch is
        # atomic: concurrent duplicate tasks wait and then skip saved vectors;
        # reprocessing/deletion can proceed between batches, never mid-write.
        with transaction.atomic():
            document = Document.objects.select_for_update().filter(public_id=public_id).first()
            if document is None:
                return {"status": "deleted", "embedded_count": embedded_count}
            if document.status != Document.Status.READY:
                return {"status": "skipped", "embedded_count": embedded_count}
            if chunk_generation(document) != generation:
                return {"status": "stale", "embedded_count": embedded_count}
            if get_embedding_profile() != profile:
                raise LocalEmbeddingConfigurationError("Embedding profile changed during processing.")
            if document.chunks.filter(embedding__isnull=False).exclude(
                embedding_model=profile.model_id,
                embedding_dimensions=profile.dimensions,
                embedding_profile_hash=profile.profile_hash,
            ).exists():
                raise LocalEmbeddingConfigurationError("Stored embedding profile mismatch.")
            chunks = list(
                document.chunks.select_for_update()
                .filter(embedding__isnull=True)
                .order_by("chunk_index")[:batch_size]
            )
            if not chunks:
                if document.processing_error.startswith(ERROR_PREFIX):
                    Document.objects.filter(pk=document.pk).update(processing_error="")
                return {
                    "status": "embedded" if embedded_count else "already_embedded",
                    "embedded_count": embedded_count,
                }
            for chunk in chunks:
                if hashlib.sha256(chunk.content.encode("utf-8")).hexdigest() != chunk.content_hash:
                    raise LocalEmbeddingInputError("Chunk content checksum mismatch.")

            service = get_local_embedding_service()
            if service.profile != profile:
                raise LocalEmbeddingConfigurationError("Loaded embedding model profile mismatch.")
            # The service adds passage: itself. Do not prefix text twice.
            vectors = service.embed_passages([chunk.content for chunk in chunks])
            _validate_vectors(vectors, len(chunks), profile.dimensions)
            embedded_at = timezone.now()
            for chunk, vector in zip(chunks, vectors, strict=True):
                chunk.embedding = vector
                chunk.embedding_model = profile.model_id
                chunk.embedding_dimensions = profile.dimensions
                chunk.embedding_profile_hash = profile.profile_hash
                chunk.embedded_at = embedded_at
            DocumentChunk.objects.bulk_update(
                chunks,
                ["embedding", "embedding_model", "embedding_dimensions", "embedding_profile_hash", "embedded_at"],
            )
        embedded_count += len(chunks)
