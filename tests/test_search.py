"""/search: cross-lecture retrieval with no LLM involved — the same corpus.search() the
chat pipeline uses, just returned directly instead of feeding a generated answer.
"""
from __future__ import annotations

import numpy as np
from fastapi.testclient import TestClient


def test_empty_query_returns_no_results_without_touching_the_encoder(monkeypatch):
    from app import embed
    from app.main import app

    def must_not_run(texts):
        raise AssertionError("the encoder should never run for an empty query")

    monkeypatch.setattr(embed, "embed_queries", must_not_run)
    with TestClient(app) as c:
        r = c.get("/search", params={"q": "   "})
    assert r.status_code == 200
    assert r.json() == {"results": []}


def test_a_real_query_returns_grounded_hits_with_timestamps(loaded_corpus, fixed_query_vector, monkeypatch):
    from app import embed
    from app.main import app

    vec = fixed_query_vector(10)  # a real committed passage vector — see conftest.py
    monkeypatch.setattr(embed, "embed_queries", lambda texts: vec)
    with TestClient(app) as c:
        r = c.get("/search", params={"q": "peak finding", "k": 5})
    assert r.status_code == 200
    results = r.json()["results"]
    assert results, "a query matching a real committed passage should return at least one hit"
    assert all({"video_id", "title", "t", "t_end", "snippet", "score"} <= r.keys() for r in results)
    assert len(results) <= 5


def test_k_is_clamped_to_a_sane_range(loaded_corpus, fixed_query_vector, monkeypatch):
    from app import embed
    from app.main import app

    vec = fixed_query_vector(10)
    monkeypatch.setattr(embed, "embed_queries", lambda texts: vec)
    with TestClient(app) as c:
        r = c.get("/search", params={"q": "peak finding", "k": 500})
    assert r.status_code == 200
    assert len(r.json()["results"]) <= 30


def test_encoder_unavailable_is_a_clean_503_not_a_500(monkeypatch):
    from app import embed
    from app.main import app

    def raises(texts):
        raise embed.EmbeddingUnavailable("no encoder")

    monkeypatch.setattr(embed, "embed_queries", raises)
    with TestClient(app) as c:
        r = c.get("/search", params={"q": "anything"})
    assert r.status_code == 503
    assert r.json()["detail"] == "encoder_unavailable"
