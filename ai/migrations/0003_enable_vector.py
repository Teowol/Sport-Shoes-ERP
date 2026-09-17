from django.db import migrations
from pgvector.django import VectorExtension


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0002_documentchunk_alter_document_status"),
    ]

    operations = [VectorExtension()]
