"""Query/passage embeddings on CPU with multilingual-e5. Loaded once per process.
e5 expects 'query: ' / 'passage: ' prefixes — keep them consistent with the pipeline."""
from __future__ import annotations

import threading

import numpy as np

from .config import settings

_model = None
_lock = threading.Lock()


def get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer

                _model = SentenceTransformer(settings.embed_model, device=settings.embed_device, model_kwargs={"use_safetensors": True})
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
