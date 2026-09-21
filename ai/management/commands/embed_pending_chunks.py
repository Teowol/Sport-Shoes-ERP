"""Resume document embeddings through the isolated embeddings worker."""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Exists, OuterRef

from ai.models import Document, DocumentChunk
from ai.services.embedding_pipeline import chunk_generation, publish_document_embeddings
from ai.services.local_embeddings import LocalEmbeddingConfigurationError, get_embedding_profile


class Command(BaseCommand):
    help = "Embed edilmemiş chunk içeren hazır dokümanları embeddings kuyruğuna gönderir."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=100, help="Tarama başına doküman sayısı (varsayılan: 100).")
        parser.add_argument("--limit", type=int, help="Bu çalıştırmada gönderilecek en fazla doküman sayısı.")
        parser.add_argument("--dry-run", action="store_true", help="Kuyruğa göndermeden bekleyen kayıtları ve profilleri kontrol et.")

    def handle(self, *args, **options):
        batch_size, limit = options["batch_size"], options["limit"]
        if batch_size < 1 or (limit is not None and limit < 1):
            raise CommandError("--batch-size ve --limit pozitif tam sayı olmalıdır.")
        dry_run = options["dry_run"]
        documents = chunks = skipped = 0
        try:
            profile = get_embedding_profile()  # Does not load the model.
            pending = Document.objects.filter(
                Exists(DocumentChunk.objects.filter(document_id=OuterRef("pk"), embedding__isnull=True)),
                status=Document.Status.READY,
            )
            # Keyset pagination keeps working when workers remove rows from the
            # pending set. A fixed ceiling excludes newly uploaded documents.
            ceiling = pending.order_by("-pk").values_list("pk", flat=True).first()
            cursor = 0
            while ceiling is not None and (limit is None or documents < limit):
                size = batch_size if limit is None else min(batch_size, limit - documents)
                ids = list(pending.filter(pk__gt=cursor, pk__lte=ceiling).order_by("pk").values_list("pk", flat=True)[:size])
                if not ids:
                    break
                for pk in ids:
                    cursor = pk
                    prepared = self._prepare_document(pk, profile)
                    if prepared is None:
                        skipped += 1
                        continue
                    public_id, generation, count = prepared
                    if not dry_run:
                        # Publish outside the row lock. The worker rechecks the
                        # generation/profile and skips any completed chunks.
                        publish_document_embeddings(public_id, generation, profile)
                    documents += 1
                    chunks += count
                    if options["verbosity"] > 1:
                        self.stdout.write(f"{'Önizleme' if dry_run else 'Kuyruğa gönderildi'}: {public_id}; bekleyen chunk={count}")
        except KeyboardInterrupt as exc:
            raise CommandError(
                "Durduruldu. Kuyruktaki görevler çalışmaya devam eder; aynı komutla devam edebilirsiniz.",
                returncode=130,
            ) from exc
        except LocalEmbeddingConfigurationError as exc:
            raise CommandError(f"Profil/yapılandırma doğrulaması başarısız: {exc}") from exc
        except Exception as exc:
            raise CommandError(
                f"Backfill durdu ({type(exc).__name__}). Bağlantıları/logları kontrol edip komutu tekrar çalıştırın; "
                "önceden gönderilmiş görevler devam edebilir."
            ) from exc
        finally:
            self.stdout.write(
                f"{'Önizleme' if dry_run else 'Gönderim'}: doküman={documents}, "
                f"bekleyen chunk={chunks}, tarama sırasında tamamlanan/değişen={skipped}."
            )
        if not dry_run and documents:
            self.stdout.write("Görevler embeddings kuyruğuna gönderildi; embedding tamamlanması worker tarafından izlenmelidir.")

    @staticmethod
    def _prepare_document(pk, profile):
        # Use the same lock as chunk replacement and embedding batches. This
        # snapshot is read-only, including in dry-run mode.
        with transaction.atomic():
            document = Document.objects.select_for_update().filter(pk=pk, status=Document.Status.READY).first()
            if document is None:
                return None
            count = document.chunks.filter(embedding__isnull=True).count()
            if not count:
                return None
            if document.chunks.filter(embedding__isnull=False).exclude(
                embedding_model=profile.model_id,
                embedding_dimensions=profile.dimensions,
                embedding_profile_hash=profile.profile_hash,
            ).exists():
                raise LocalEmbeddingConfigurationError(f"Doküman {document.public_id}: kayıtlı embedding profili uyuşmuyor.")
            return document.public_id, chunk_generation(document), count
