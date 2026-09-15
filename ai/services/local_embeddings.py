"""Fail-closed, CPU-only local embeddings for document content."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# This must be set before fastembed imports ONNX Runtime.
os.environ["ORT_DISABLE_TELEMETRY"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"

import numpy as np
from django.conf import settings
from tokenizers import Tokenizer


QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "
WINDOW_CONTENT_TOKENS = 480
WINDOW_OVERLAP_TOKENS = 64
NORM_ABS_TOLERANCE = 1e-4


class LocalEmbeddingError(RuntimeError):
    """Base error for local embedding failures."""


class LocalEmbeddingConfigurationError(LocalEmbeddingError):
    """Raised when the pinned local model configuration is unsafe or invalid."""


class LocalEmbeddingInputError(LocalEmbeddingError):
    """Raised when input cannot be embedded without violating model limits."""


@dataclass(frozen=True)
class ApprovedModel:
    dimension: int
    max_tokens: int
    checksums: dict[str, str]


APPROVED_MODELS = {
    (
        "intfloat/multilingual-e5-small",
        "614241f622f53c4eeff9890bdc4f31cfecc418b3",
    ): ApprovedModel(
        dimension=384,
        max_tokens=512,
        checksums={
            "1_Pooling/config.json": "987f7a67a38fa564c849bb5d277c52ab9088a84368fc0be31a354125aebb12a0",
            "config.json": "69137736cab8b8903a07fe8afaafdda25aac55415a12a55d1bffa9f581abf959",
            "modules.json": "c6e29747481e8b5dd2b58401966aeac910de39092f90cda9a704b1545f902b04",
            "onnx/config.json": "bbb7c1333fc4b3e27fbc9cd5d2070aabcc1d4dfb99917c3633e772f97545a6b6",
            "onnx/model.onnx": "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665",
            "sentence_bert_config.json": "948201d8329907aae938fa62f9ceeed53f5694dacc2b87b9f3b78b37ee986529",
            "sentencepiece.bpe.model": "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
            "special_tokens_map.json": "d05497f1da52c5e09554c0cd874037a083e1dc1b9cfd48034d1c717f1afc07a7",
            "tokenizer.json": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
            "tokenizer_config.json": "a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b",
        },
    )
}

_backend_registration_lock = threading.Lock()
_registered_backend_models: set[str] = set()


class LocalEmbeddingService:
    """Generate normalized E5 embeddings without network or OpenAI access."""

    def __init__(self) -> None:
        self.provider = settings.AI_EMBEDDING_PROVIDER
        self.model_id = settings.AI_EMBEDDING_MODEL_ID
        self.revision = settings.AI_EMBEDDING_MODEL_REVISION
        self.dimension = settings.AI_EMBEDDING_DIMENSION
        self.batch_size = settings.AI_EMBEDDING_BATCH_SIZE
        self.threads = settings.AI_EMBEDDING_THREADS
        self.offline = settings.AI_EMBEDDING_OFFLINE
        self.max_model_tokens = settings.AI_EMBEDDING_MAX_MODEL_TOKENS
        self._inference_lock = threading.Lock()

        self._approved_model = self._validate_configuration()
        self.model_path = self._validate_model_path()
        self._tokenizer = self._load_raw_tokenizer()
        self._backend = self._create_backend()
        self.execution_providers = self._validate_execution_provider()

        # Validate the model's real output shape and normalization at startup.
        self._embed_prefixed([f"{PASSAGE_PREFIX}dimension probe"])

    def _validate_configuration(self) -> ApprovedModel:
        if self.provider != "local_fastembed":
            raise LocalEmbeddingConfigurationError(
                "Unsupported local embedding provider."
            )
        approved = APPROVED_MODELS.get((self.model_id, self.revision))
        if approved is None:
            raise LocalEmbeddingConfigurationError(
                "Embedding model or revision is not approved."
            )
        if not self.offline:
            raise LocalEmbeddingConfigurationError(
                "Local embedding service must run in offline mode."
            )
        if self.dimension != approved.dimension:
            raise LocalEmbeddingConfigurationError(
                "Configured embedding dimension does not match the pinned model."
            )
        if self.max_model_tokens != approved.max_tokens:
            raise LocalEmbeddingConfigurationError(
                "Configured token limit does not match the pinned model."
            )
        if self.batch_size < 1 or self.threads < 1:
            raise LocalEmbeddingConfigurationError(
                "Embedding batch size and thread count must be positive."
            )
        if WINDOW_CONTENT_TOKENS + 4 > self.max_model_tokens:
            raise LocalEmbeddingConfigurationError(
                "Embedding window is unsafe for the configured token limit."
            )
        if not 0 <= WINDOW_OVERLAP_TOKENS < WINDOW_CONTENT_TOKENS:
            raise LocalEmbeddingConfigurationError(
                "Embedding overlap configuration is invalid."
            )
        return approved

    def _validate_model_path(self) -> Path:
        configured_path = Path(settings.AI_EMBEDDING_MODEL_PATH)
        if not configured_path.is_absolute():
            raise LocalEmbeddingConfigurationError(
                "Embedding model path must be absolute."
            )
        if configured_path.is_symlink():
            raise LocalEmbeddingConfigurationError(
                "Embedding model path must not be a symbolic link."
            )
        try:
            model_path = configured_path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise LocalEmbeddingConfigurationError(
                "Pinned local embedding model is unavailable."
            ) from exc
        if not model_path.is_dir():
            raise LocalEmbeddingConfigurationError(
                "Embedding model path is not a directory."
            )

        for relative_name, expected_checksum in self._approved_model.checksums.items():
            artifact = model_path / relative_name
            try:
                relative_artifact = artifact.relative_to(model_path)
            except ValueError as exc:
                raise LocalEmbeddingConfigurationError(
                    "Embedding artifact path is invalid."
                ) from exc
            current = model_path
            for part in relative_artifact.parts:
                current = current / part
                if current.is_symlink():
                    raise LocalEmbeddingConfigurationError(
                        "Embedding artifacts must not be symbolic links."
                    )
            try:
                resolved_artifact = artifact.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise LocalEmbeddingConfigurationError(
                    "Pinned embedding artifact is missing."
                ) from exc
            if not resolved_artifact.is_relative_to(model_path) or not artifact.is_file():
                raise LocalEmbeddingConfigurationError(
                    "Embedding artifact path is invalid."
                )
            if self._sha256(artifact) != expected_checksum:
                raise LocalEmbeddingConfigurationError(
                    "Pinned embedding artifact checksum mismatch."
                )

        self._validate_model_metadata(model_path)
        return model_path

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as artifact:
                for block in iter(lambda: artifact.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise LocalEmbeddingConfigurationError(
                "Embedding artifact could not be read."
            ) from exc
        return digest.hexdigest()

    def _validate_model_metadata(self, model_path: Path) -> None:
        try:
            model_config = json.loads(
                (model_path / "config.json").read_text(encoding="utf-8")
            )
            tokenizer_config = json.loads(
                (model_path / "tokenizer_config.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LocalEmbeddingConfigurationError(
                "Embedding model metadata is invalid."
            ) from exc
        if model_config.get("hidden_size") != self.dimension:
            raise LocalEmbeddingConfigurationError(
                "Embedding model metadata dimension mismatch."
            )
        if tokenizer_config.get("model_max_length") != self.max_model_tokens:
            raise LocalEmbeddingConfigurationError(
                "Embedding tokenizer limit mismatch."
            )

    def _load_raw_tokenizer(self) -> Tokenizer:
        try:
            tokenizer = Tokenizer.from_file(str(self.model_path / "tokenizer.json"))
            tokenizer.no_truncation()
            tokenizer.no_padding()
        except Exception as exc:
            raise LocalEmbeddingConfigurationError(
                "Pinned embedding tokenizer could not be loaded."
            ) from exc
        return tokenizer

    def _create_backend(self):
        try:
            from fastembed import TextEmbedding
            from fastembed.common.model_description import ModelSource, PoolingType
        except ImportError as exc:
            raise LocalEmbeddingConfigurationError(
                "Local embedding runtime is unavailable."
            ) from exc

        backend_model_name = (
            f"erp-local-{self.model_id.replace('/', '-')}-{self.revision[:12]}"
        )
        with _backend_registration_lock:
            if backend_model_name not in _registered_backend_models:
                try:
                    TextEmbedding.add_custom_model(
                        model=backend_model_name,
                        pooling=PoolingType.MEAN,
                        normalization=True,
                        sources=ModelSource(hf=self.model_id),
                        dim=self.dimension,
                        model_file="onnx/model.onnx",
                        description="Pinned local multilingual E5 embedding model",
                        license="mit",
                        size_in_gb=0.47,
                    )
                except ValueError as exc:
                    raise LocalEmbeddingConfigurationError(
                        "Local embedding model registration failed."
                    ) from exc
                _registered_backend_models.add(backend_model_name)

        try:
            return TextEmbedding(
                model_name=backend_model_name,
                cache_dir=str(self.model_path.parent / ".unused-fastembed-cache"),
                threads=self.threads,
                providers=["CPUExecutionProvider"],
                local_files_only=True,
                specific_model_path=str(self.model_path),
            )
        except Exception as exc:
            raise LocalEmbeddingConfigurationError(
                "Pinned local embedding model could not be loaded."
            ) from exc

    def _validate_execution_provider(self) -> tuple[str, ...]:
        try:
            providers = tuple(self._backend.model.model.get_providers())
        except (AttributeError, TypeError) as exc:
            raise LocalEmbeddingConfigurationError(
                "Embedding execution provider could not be verified."
            ) from exc
        if providers != ("CPUExecutionProvider",):
            raise LocalEmbeddingConfigurationError(
                "Embedding runtime is not CPU-only."
            )
        return providers

    def _token_count(self, text: str, *, add_special_tokens: bool) -> int:
        try:
            return len(
                self._tokenizer.encode(
                    text, add_special_tokens=add_special_tokens
                ).ids
            )
        except Exception as exc:
            raise LocalEmbeddingInputError(
                "Embedding input could not be tokenized."
            ) from exc

    def _clean_input(self, text: str) -> str:
        if not isinstance(text, str):
            raise LocalEmbeddingInputError("Embedding input must be text.")
        cleaned = text.strip()
        if not cleaned:
            raise LocalEmbeddingInputError("Embedding input must not be empty.")
        return cleaned

    def _check_model_input(self, prefixed_text: str) -> int:
        count = self._token_count(prefixed_text, add_special_tokens=True)
        if count > self.max_model_tokens:
            raise LocalEmbeddingInputError(
                "Embedding input exceeds the model token limit."
            )
        return count

    def _validate_output(self, value) -> np.ndarray:
        vector = np.asarray(value, dtype=np.float32)
        if vector.shape != (self.dimension,):
            raise LocalEmbeddingConfigurationError(
                "Embedding output dimension mismatch."
            )
        if not np.isfinite(vector).all():
            raise LocalEmbeddingConfigurationError(
                "Embedding output contains non-finite values."
            )
        norm = float(np.linalg.norm(vector))
        if not math.isfinite(norm) or norm <= 0:
            raise LocalEmbeddingConfigurationError(
                "Embedding output norm is invalid."
            )
        if not math.isclose(norm, 1.0, abs_tol=NORM_ABS_TOLERANCE):
            raise LocalEmbeddingConfigurationError(
                "Embedding output is not normalized."
            )
        return vector

    def _embed_prefixed(self, texts: Sequence[str]) -> list[np.ndarray]:
        if not texts:
            return []
        for text in texts:
            self._check_model_input(text)
        try:
            with self._inference_lock:
                raw_vectors = list(
                    self._backend.embed(texts, batch_size=self.batch_size)
                )
        except LocalEmbeddingError:
            raise
        except Exception as exc:
            raise LocalEmbeddingError("Local embedding inference failed.") from exc
        if len(raw_vectors) != len(texts):
            raise LocalEmbeddingConfigurationError(
                "Embedding runtime returned an unexpected result count."
            )
        return [self._validate_output(vector) for vector in raw_vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_queries([text])[0]

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        cleaned = [self._clean_input(text) for text in texts]
        prefixed = [f"{QUERY_PREFIX}{text}" for text in cleaned]
        vectors = self._embed_prefixed(prefixed)
        return [vector.tolist() for vector in vectors]

    def embed_passage(self, text: str) -> list[float]:
        return self.embed_passages([text])[0]

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        cleaned = [self._clean_input(text) for text in texts]
        windows: list[str] = []
        plans: list[tuple[int, int, list[int]]] = []

        for text in cleaned:
            start_index = len(windows)
            passage_windows, weights = self._passage_windows(text)
            windows.extend(passage_windows)
            plans.append((start_index, len(passage_windows), weights))

        window_vectors = self._embed_prefixed(windows)
        results: list[list[float]] = []
        for start_index, window_count, weights in plans:
            vectors = np.asarray(
                window_vectors[start_index : start_index + window_count],
                dtype=np.float32,
            )
            aggregate = np.average(
                vectors,
                axis=0,
                weights=np.asarray(weights, dtype=np.float32),
            )
            norm = float(np.linalg.norm(aggregate))
            if not math.isfinite(norm) or norm <= 0:
                raise LocalEmbeddingConfigurationError(
                    "Aggregated embedding norm is invalid."
                )
            aggregate = np.asarray(aggregate / norm, dtype=np.float32)
            results.append(self._validate_output(aggregate).tolist())
        return results

    def _passage_windows(self, text: str) -> tuple[list[str], list[int]]:
        prefixed = f"{PASSAGE_PREFIX}{text}"
        if self._token_count(prefixed, add_special_tokens=True) <= self.max_model_tokens:
            return [prefixed], [1]

        try:
            content_ids = self._tokenizer.encode(
                text, add_special_tokens=False
            ).ids
        except Exception as exc:
            raise LocalEmbeddingInputError(
                "Embedding passage could not be tokenized."
            ) from exc
        if not content_ids:
            raise LocalEmbeddingInputError(
                "Embedding passage has no meaningful tokens."
            )

        windows: list[str] = []
        weights: list[int] = []
        start = 0
        while start < len(content_ids):
            end = min(start + WINDOW_CONTENT_TOKENS, len(content_ids))
            while end > start:
                window_text = self._tokenizer.decode(content_ids[start:end]).strip()
                if window_text and (
                    self._token_count(
                        f"{PASSAGE_PREFIX}{window_text}",
                        add_special_tokens=True,
                    )
                    <= self.max_model_tokens
                ):
                    break
                end -= 1
            if end <= start:
                raise LocalEmbeddingInputError(
                    "Embedding passage cannot be safely windowed."
                )

            windows.append(f"{PASSAGE_PREFIX}{window_text}")
            token_count = end - start
            weights.append(
                token_count
                if not weights
                else max(1, token_count - WINDOW_OVERLAP_TOKENS)
            )
            if end == len(content_ids):
                break
            next_start = end - WINDOW_OVERLAP_TOKENS
            if next_start <= start:
                raise LocalEmbeddingInputError(
                    "Embedding passage windowing did not advance."
                )
            start = next_start

        return windows, weights


_service_instance: LocalEmbeddingService | None = None
_service_lock = threading.Lock()


def get_local_embedding_service() -> LocalEmbeddingService:
    """Return the single local model instance for the current process."""
    global _service_instance
    if _service_instance is None:
        with _service_lock:
            if _service_instance is None:
                _service_instance = LocalEmbeddingService()
    return _service_instance


def _reset_local_embedding_service_for_tests() -> None:
    global _service_instance
    with _service_lock:
        _service_instance = None
