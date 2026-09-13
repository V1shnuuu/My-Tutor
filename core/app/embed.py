"""Query/passage embeddings on CPU with multilingual-e5. Loaded once per process.
e5 expects 'query: ' / 'passage: ' prefixes — keep them consistent with the pipeline."""
from __future__ import annotations

import threading
import time

import numpy as np

from .config import settings


class EmbeddingUnavailable(RuntimeError):
    """The encoder could not be loaded — not downloaded yet, no network, or out of memory."""


_model = None
_lock = threading.Lock()
_failed_at = 0.0
# A failed load costs ~90s of retries inside huggingface_hub before it gives up. Paying that
# on every question turns "the model is missing" into "the tutor hangs forever", so remember
# the failure and answer immediately until it is worth another attempt. The warmup at startup
# means the slow attempt normally happens at boot, not inside a student's request.
_RETRY_AFTER_S = 300.0


def get_model():
    global _model, _failed_at
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:
            return _model
        if _failed_at and time.monotonic() - _failed_at < _RETRY_AFTER_S:
            raise EmbeddingUnavailable(f"encoder unavailable; retrying in {int(_RETRY_AFTER_S - (time.monotonic() - _failed_at))}s")
        try:
            from sentence_transformers import SentenceTransformer

            _model = SentenceTransformer(settings.embed_model, device=settings.embed_device, model_kwargs={"use_safetensors": True})
        except Exception as e:
            _failed_at = time.monotonic()
            raise EmbeddingUnavailable(f"could not load {settings.embed_model}: {e}") from e
        _failed_at = 0.0
    return _model


def embed_queries(texts: list[str]) -> np.ndarray:
    m = get_model()
    vecs = m.encode([f"query: {t}" for t in texts], normalize_embeddings=True, batch_size=16)
    return np.asarray(vecs, dtype=np.float32)


def embed_passages(texts: list[str]) -> np.ndarray:
    m = get_model()
    vecs = m.encode(
        [f"passage: {t}" for t in texts], normalize_embeddings=True, batch_size=16, show_progress_bar=True
    )
    return np.asarray(vecs, dtype=np.float32)
