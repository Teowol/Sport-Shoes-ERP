"""Read-only, profile-compatible pgvector search over authorized documents."""

import math

from django.core.exceptions import PermissionDenied
from pgvector.django import CosineDistance

from ai.models import Document, DocumentChunk
from ai.permissions import can_search_documents
from .local_embeddings import (
    LocalEmbeddingConfigurationError,
    LocalEmbeddingInputError,
    NORM_ABS_TOLERANCE,
    get_embedding_profile,
    get_local_embedding_service,
)


DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 10
MAX_QUERY_CHARACTERS = 2000


def search_document_chunks(user, query, limit=DEFAULT_SEARCH_LIMIT):
    """Return only citation fields and text; never serialize model instances."""
    # Keep this guard in the service too: direct calls cannot bypass the tool.
    if not can_search_documents(user):
        raise PermissionDenied
    if not isinstance(query, str) or len(query) > MAX_QUERY_CHARACTERS:
        raise LocalEmbeddingInputError("Invalid search query.")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise LocalEmbeddingInputError("Invalid search limit.")
    query = query.strip()
    if not query:
        return []
    limit = min(limit, MAX_SEARCH_LIMIT)
    profile = get_embedding_profile()

    # Current access is collection-wide for authorized factory users, not
    # uploader ownership. Buyer membership is denied by the guard above.
    # Keep document scope in SQL, before ranking/LIMIT, for future ACL filters.
    accessible_documents = Document.objects.filter(status=Document.Status.READY)
    chunks = DocumentChunk.objects.filter(
        document__in=accessible_documents,
        embedding__isnull=False,
        embedded_at__isnull=False,
        embedding_model=profile.model_id,
        embedding_dimensions=profile.dimensions,
        embedding_profile_hash=profile.profile_hash,
    )
    if not chunks.exists():
        return []

    service = get_local_embedding_service()
    if service.profile != profile:
        raise LocalEmbeddingConfigurationError("Loaded query embedding profile mismatch.")
    # embed_query adds query: internally; passing it here would prefix twice.
    vector = service.embed_query(query)
    if (
        len(vector) != profile.dimensions
        or not all(math.isfinite(value) for value in vector)
        or not math.isclose(math.sqrt(sum(value * value for value in vector)), 1.0, abs_tol=NORM_ABS_TOLERANCE)
    ):
        raise LocalEmbeddingConfigurationError("Invalid query embedding vector.")
    if get_embedding_profile() != profile or service.profile != profile:
        raise LocalEmbeddingConfigurationError("Query embedding profile changed during search.")

    matches = (
        chunks.annotate(distance=CosineDistance("embedding", vector))
        # Zero vectors have an undefined cosine distance. Never return NaN or
        # let invalid stored vectors displace valid results within the limit.
        .filter(distance__gte=0.0, distance__lte=2.0)
        .order_by("distance", "document_id", "chunk_index")
        .values("document__title", "content", "page_number", "section_title", "distance")[:limit]
    )
    return [
        {
            "source_type": "document",
            "document_name": row["document__title"],
            "content": row["content"],
            "page_number": row["page_number"],
            "section_title": row["section_title"],
            "cosine_distance": row["distance"],
        }
        for row in matches
    ]
