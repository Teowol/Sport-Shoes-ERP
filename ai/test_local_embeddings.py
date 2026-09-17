from __future__ import annotations

import hashlib
import math
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
from django.test import SimpleTestCase, override_settings

from ai.services import local_embeddings
from ai.services.local_embeddings import (
    APPROVED_MODELS,
    PASSAGE_PREFIX,
    QUERY_PREFIX,
    ApprovedModel,
    LocalEmbeddingConfigurationError,
    LocalEmbeddingInputError,
    LocalEmbeddingService,
)


MODEL_ID = "intfloat/multilingual-e5-small"
MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"


class FakeEncoding:
    def __init__(self, ids):
        self.ids = ids


class FakeTokenizer:
    def encode(self, text, add_special_tokens=True):
        ids = list(range(1, len(text.split()) + 1))
        if add_special_tokens:
            ids = [101, *ids, 102]
        return FakeEncoding(ids)

    def decode(self, ids):
        return " ".join(f"token-{item}" for item in ids)


class FakeSession:
    def __init__(self, providers=None):
        self.providers = providers or ["CPUExecutionProvider"]

    def get_providers(self):
        return self.providers


class FakeBackend:
    def __init__(self, *, providers=None, invalid_output=None):
        self.model = SimpleNamespace(model=FakeSession(providers))
        self.calls = []
        self.invalid_output = invalid_output

    def embed(self, texts, batch_size):
        self.calls.append((list(texts), batch_size))
        for text in texts:
            if self.invalid_output is not None:
                yield self.invalid_output
                continue
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vector = np.zeros(384, dtype=np.float32)
            vector[0] = 1.0
            vector[1 : 1 + len(digest)] = np.frombuffer(
                digest, dtype=np.uint8
            ).astype(np.float32) / 255.0
            vector /= np.linalg.norm(vector)
            yield vector


EMBEDDING_SETTINGS = {
    "AI_EMBEDDING_PROVIDER": "local_fastembed",
    "AI_EMBEDDING_MODEL_ID": MODEL_ID,
    "AI_EMBEDDING_MODEL_REVISION": MODEL_REVISION,
    "AI_EMBEDDING_MODEL_PATH": "/tmp/test-local-embedding-model",
    "AI_EMBEDDING_DIMENSION": 384,
    "AI_EMBEDDING_BATCH_SIZE": 4,
    "AI_EMBEDDING_THREADS": 1,
    "AI_EMBEDDING_OFFLINE": True,
    "AI_EMBEDDING_MAX_MODEL_TOKENS": 512,
}


@override_settings(**EMBEDDING_SETTINGS)
class LocalEmbeddingServiceTests(SimpleTestCase):
    def make_service(self, *, backend=None):
        backend = backend or FakeBackend()
        with (
            mock.patch.object(
                LocalEmbeddingService,
                "_validate_model_path",
                return_value=Path("/tmp/test-local-embedding-model"),
            ),
            mock.patch.object(
                LocalEmbeddingService,
                "_load_raw_tokenizer",
                return_value=FakeTokenizer(),
            ),
            mock.patch.object(
                LocalEmbeddingService,
                "_create_backend",
                return_value=backend,
            ),
        ):
            service = LocalEmbeddingService()
        backend.calls.clear()
        return service, backend

    def test_cpu_provider_dimension_finite_and_norm(self):
        service, _ = self.make_service()

        vector = np.asarray(service.embed_query("depo stoğu"), dtype=np.float32)

        self.assertEqual(service.execution_providers, ("CPUExecutionProvider",))
        self.assertEqual(vector.shape, (384,))
        self.assertTrue(np.isfinite(vector).all())
        self.assertTrue(math.isclose(float(np.linalg.norm(vector)), 1.0, abs_tol=1e-4))

    def test_non_cpu_provider_is_rejected(self):
        with self.assertRaises(LocalEmbeddingConfigurationError):
            self.make_service(
                backend=FakeBackend(
                    providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
                )
            )

    def test_query_and_passage_prefixes_are_applied_centrally(self):
        service, backend = self.make_service()

        service.embed_query("kırmızı kumaş")
        service.embed_passage("en eski lot önce kullanılır")

        self.assertTrue(backend.calls[0][0][0].startswith(QUERY_PREFIX))
        self.assertTrue(backend.calls[1][0][0].startswith(PASSAGE_PREFIX))

    def test_empty_input_is_rejected(self):
        service, _ = self.make_service()

        with self.assertRaises(LocalEmbeddingInputError):
            service.embed_query("   ")
        with self.assertRaises(LocalEmbeddingInputError):
            service.embed_passage("")

    def test_query_token_limit_is_checked_without_truncation(self):
        service, backend = self.make_service()
        boundary_text = " ".join(["word"] * 509)
        too_long_text = " ".join(["word"] * 510)

        vector = service.embed_query(boundary_text)
        self.assertEqual(len(vector), 384)
        with self.assertRaises(LocalEmbeddingInputError):
            service.embed_query(too_long_text)
        self.assertEqual(len(backend.calls), 1)

    def test_long_passage_uses_weighted_windows_and_renormalizes(self):
        service, backend = self.make_service()
        long_text = " ".join(f"word-{index}" for index in range(760))

        vector = np.asarray(service.embed_passage(long_text), dtype=np.float32)

        embedded_windows = backend.calls[0][0]
        self.assertEqual(len(embedded_windows), 2)
        self.assertTrue(all(text.startswith(PASSAGE_PREFIX) for text in embedded_windows))
        self.assertTrue(
            all(
                service._token_count(text, add_special_tokens=True) <= 512
                for text in embedded_windows
            )
        )
        self.assertEqual(vector.shape, (384,))
        self.assertTrue(np.isfinite(vector).all())
        self.assertTrue(math.isclose(float(np.linalg.norm(vector)), 1.0, abs_tol=1e-4))

    def test_missing_model_path_fails_closed(self):
        with override_settings(
            AI_EMBEDDING_MODEL_PATH="/tmp/model-path-that-does-not-exist"
        ):
            with self.assertRaises(LocalEmbeddingConfigurationError):
                LocalEmbeddingService()

    def test_revision_mismatch_fails_closed(self):
        with override_settings(AI_EMBEDDING_MODEL_REVISION="unapproved-revision"):
            with self.assertRaises(LocalEmbeddingConfigurationError):
                LocalEmbeddingService()

    def test_dimension_mismatch_fails_closed(self):
        with override_settings(AI_EMBEDDING_DIMENSION=768):
            with self.assertRaises(LocalEmbeddingConfigurationError):
                LocalEmbeddingService()

    def test_offline_mode_cannot_be_disabled(self):
        with override_settings(AI_EMBEDDING_OFFLINE=False):
            with self.assertRaises(LocalEmbeddingConfigurationError):
                LocalEmbeddingService()

    def test_artifact_checksum_mismatch_fails_closed(self):
        key = (MODEL_ID, MODEL_REVISION)
        approved = ApprovedModel(
            dimension=384,
            max_tokens=512,
            checksums={"tokenizer.json": "0" * 64},
        )
        with tempfile.TemporaryDirectory() as model_dir:
            Path(model_dir, "tokenizer.json").write_text("{}", encoding="utf-8")
            with (
                override_settings(AI_EMBEDDING_MODEL_PATH=model_dir),
                mock.patch.dict(APPROVED_MODELS, {key: approved}, clear=True),
            ):
                with self.assertRaises(LocalEmbeddingConfigurationError):
                    LocalEmbeddingService()

    def test_output_is_deterministic(self):
        service, _ = self.make_service()

        first = service.embed_query("deterministic query")
        second = service.embed_query("deterministic query")

        np.testing.assert_allclose(first, second, atol=0.0, rtol=0.0)

    def test_non_finite_output_fails_closed(self):
        invalid = np.full(384, np.nan, dtype=np.float32)
        with self.assertRaises(LocalEmbeddingConfigurationError):
            self.make_service(backend=FakeBackend(invalid_output=invalid))

    def test_openai_embeddings_are_never_called(self):
        service, _ = self.make_service()

        with mock.patch("openai.resources.embeddings.Embeddings.create") as create:
            service.embed_query("yerel sorgu")

        create.assert_not_called()

    def test_backend_is_loaded_from_local_path_with_cpu_only(self):
        captured = {}

        class FakeTextEmbedding(FakeBackend):
            @classmethod
            def add_custom_model(cls, **kwargs):
                captured["registration"] = kwargs

            def __init__(self, **kwargs):
                super().__init__()
                captured["load"] = kwargs

        local_embeddings._registered_backend_models.clear()
        with (
            mock.patch.object(
                LocalEmbeddingService,
                "_validate_model_path",
                return_value=Path("/tmp/test-local-embedding-model"),
            ),
            mock.patch.object(
                LocalEmbeddingService,
                "_load_raw_tokenizer",
                return_value=FakeTokenizer(),
            ),
            mock.patch("fastembed.TextEmbedding", FakeTextEmbedding),
        ):
            LocalEmbeddingService()

        self.assertTrue(captured["load"]["local_files_only"])
        self.assertEqual(captured["load"]["providers"], ["CPUExecutionProvider"])
        self.assertEqual(
            captured["load"]["specific_model_path"],
            str(Path("/tmp/test-local-embedding-model")),
        )
        self.assertEqual(captured["load"]["threads"], 1)
        self.assertEqual(captured["registration"]["dim"], 384)
        self.assertEqual(os.environ["ORT_DISABLE_TELEMETRY"], "1")
        self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")

    def test_process_singleton_loads_model_once(self):
        local_embeddings._reset_local_embedding_service_for_tests()
        service, _ = self.make_service()

        with mock.patch(
            "ai.services.local_embeddings.LocalEmbeddingService",
            return_value=service,
        ) as service_class:
            first = local_embeddings.get_local_embedding_service()
            second = local_embeddings.get_local_embedding_service()

        self.assertIs(first, second)
        service_class.assert_called_once()
        local_embeddings._reset_local_embedding_service_for_tests()
