"""Transaction-safe dispatch for AI document processing."""

import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Document
from .tasks import process_document


logger = logging.getLogger(__name__)


def dispatch_document_processing(public_ids) -> None:
    """Dispatch public identifiers only; database primary keys never leave the app."""
    identifiers = tuple(str(public_id) for public_id in public_ids)

    def dispatch():
        for public_id in identifiers:
            try:
                process_document.delay(public_id)
            except Exception:
                logger.exception("Document task could not be queued for %s", public_id)

    transaction.on_commit(dispatch)


@receiver(post_save, sender=Document)
def queue_new_document(sender, instance, created, **kwargs):
    if created and instance.status == Document.Status.QUEUED:
        dispatch_document_processing((instance.public_id,))
