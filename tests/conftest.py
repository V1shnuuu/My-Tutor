"""Shared fixtures.

Everything here keeps a test run off the developer's real state: a throwaway DATA_DIR
(so the SQLite file, cache and usage counters are per-run) and no provider keys, so a
stray network call would fail loudly rather than quietly billing someone.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "pipeline"))

# Must be set before app.config is imported: Settings reads them at construction.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="tutor-test-"))
os.environ.setdefault("JWT_SECRET", "test-secret-not-used-anywhere-real")
os.environ.setdefault("ADMIN_TOKEN", "test-admin")
for key in ("GROQ_API_KEY", "GEMINI_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY",
            "OPENROUTER_API_KEY", "CLOUDFLARE_API_TOKEN", "LIVEAVATAR_API_KEY"):
    os.environ[key] = ""


@pytest.fixture(scope="session")
def settings():
    from app.config import settings as s

    return s


@pytest.fixture(scope="session")
def loaded_corpus():
    """The sample lecture committed in corpus/ — real chunks, real 768-dim vectors."""
    from app.corpus import corpus

    corpus.load()
    if corpus.size == 0:
        pytest.skip("corpus is empty; run pipeline/ingest.py")
    return corpus


@pytest.fixture
def fixed_query_vector(loaded_corpus):
    """A query vector for a chosen chunk.

    The encoder's weights are a large download and a slow load, and none of these tests
    are about the encoder — they are about what happens to a vector once it exists. A
    committed passage vector is exactly what a perfect encoder would return for that
    passage, which makes it the sharpest available stand-in.
    """
    import numpy as np

    def _vec(chunk_index: int):
        return np.repeat(loaded_corpus.vecs[chunk_index: chunk_index + 1].astype(np.float32), 1, axis=0)

    return _vec
