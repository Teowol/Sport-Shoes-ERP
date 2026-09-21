"""Models for private documents managed by the AI application."""

import uuid
from pathlib import PurePosixPath

from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone
from pgvector.django import VectorField

from .document_validation import validate_document_upload


def document_upload_path(instance, filename: str) -> str:
    """Build a storage path without incorporating the untrusted filename."""
    extension = PurePosixPath((filename or "").replace("\\", "/")).suffix.lower()
    now = timezone.now()
    return f"ai/documents/{now:%Y/%m}/{instance.public_id}{extension}"


class Document(models.Model):
    """A validated, privately stored source document for the future RAG layer."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Kuyrukta"
        UPLOADED = "uploaded", "Yüklendi"
        PROCESSING = "processing", "İşleniyor"
        READY = "ready", "Hazır"
        FAILED = "failed", "Başarısız"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    title = models.CharField(max_length=255, verbose_name="Başlık")
    file = models.FileField(upload_to=document_upload_path, max_length=500, verbose_name="Dosya")
    original_filename = models.CharField(max_length=255, editable=False, verbose_name="Orijinal Dosya Adı")
    mime_type = models.CharField(max_length=127, editable=False, verbose_name="MIME Türü")
    file_size = models.PositiveBigIntegerField(editable=False, verbose_name="Dosya Boyutu")
    checksum_sha256 = models.CharField(
        max_length=64,
        db_index=True,
        editable=False,
        verbose_name="SHA-256",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.QUEUED,
        editable=False,
        verbose_name="Durum",
    )
    processing_error = models.TextField(blank=True, editable=False, verbose_name="İşleme Hatası")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="ai_documents",
        verbose_name="Yükleyen",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Oluşturulma Tarihi")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Güncellenme Tarihi")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "AI Dokümanı"
        verbose_name_plural = "AI Dokümanları"

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        if not self.file:
            return
        if self.pk and getattr(self.file, "_committed", True) and self.checksum_sha256:
            return
        metadata = validate_document_upload(self.file)
        self.original_filename = metadata.original_filename
        self.mime_type = metadata.mime_type
        self.file_size = metadata.file_size
        self.checksum_sha256 = metadata.checksum_sha256

    def save(self, *args, **kwargs):
        if self.file and (not self.pk or not getattr(self.file, "_committed", True) or not self.checksum_sha256):
            self.full_clean()
        return super().save(*args, **kwargs)


class DocumentChunk(models.Model):
    """A normalized, ordered section of a private source document."""

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="chunks",
        verbose_name="Doküman",
    )
    chunk_index = models.PositiveIntegerField(verbose_name="Parça Sırası")
    content = models.TextField(verbose_name="İçerik")
    token_count = models.PositiveIntegerField(verbose_name="Token Sayısı")
    page_number = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="Sayfa Numarası",
    )
    section_title = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Bölüm Başlığı",
    )
    content_hash = models.CharField(max_length=64, verbose_name="İçerik SHA-256")
    embedding = VectorField(
        dimensions=384,
        null=True,
        blank=True,
        editable=False,
        verbose_name="Embedding",
    )
    embedding_model = models.CharField(
        max_length=255,
        default="",
        blank=True,
        editable=False,
        verbose_name="Embedding Modeli",
    )
    embedding_dimensions = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        editable=False,
        verbose_name="Embedding Boyutu",
    )
    embedding_profile_hash = models.CharField(
        max_length=64,
        default="",
        blank=True,
        editable=False,
        verbose_name="Embedding Profil SHA-256",
    )
    embedded_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name="Embedding Oluşturulma Tarihi",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Oluşturulma Tarihi")

    class Meta:
        ordering = ["document_id", "chunk_index"]
        constraints = [
            models.UniqueConstraint(
                fields=("document", "chunk_index"),
                name="uniq_ai_document_chunk_index",
            ),
            models.UniqueConstraint(
                fields=("document", "content_hash"),
                name="uniq_ai_document_chunk_content_hash",
            ),
            # Pending chunks have no embedding metadata. A completed embedding
            # and all of its metadata must be written together, including via
            # bulk operations that bypass model validation.
            models.CheckConstraint(
                name="ai_chunk_embedding_complete",
                condition=(
                    models.Q(
                        embedding__isnull=True,
                        embedding_model="",
                        embedding_dimensions__isnull=True,
                        embedding_profile_hash="",
                        embedded_at__isnull=True,
                    )
                    | (
                        models.Q(
                            embedding__isnull=False,
                            embedding_dimensions__isnull=False,
                            embedding_dimensions=384,
                            embedding_profile_hash__regex=r"^[0-9a-f]{64}$",
                            embedded_at__isnull=False,
                        )
                        & ~models.Q(embedding_model="")
                    )
                ),
            ),
        ]
        verbose_name = "AI Doküman Parçası"
        verbose_name_plural = "AI Doküman Parçaları"

    def __str__(self):
        return f"{self.document} / {self.chunk_index}"


@receiver(post_delete, sender=Document)
def delete_document_file(sender, instance, **kwargs):
    """Remove the private file after its database row is deleted."""
    if instance.file and instance.file.name:
        instance.file.storage.delete(instance.file.name)
