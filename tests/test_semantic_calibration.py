"""Guards the semantic-cache threshold against the real embedding model.

If a model or threshold change lets a different intent clear the threshold, this fails.
Skipped when fastembed isn't installed (uv sync --extra embeddings).
"""

import pytest

from app.cache.semantic import normalise_prompt
from app.core.config import Settings

fastembed = pytest.importorskip("fastembed")

ANCHOR = "Repeat my last Swiggy order"
MUST_MISS = [  # different action, site or parameters
    "Cancel my last Swiggy order",
    "Show my last Swiggy order",
    "Rate my last Swiggy order",
    "Repeat my last Zomato order",
    "Repeat my last Swiggy order to my office address",
    "Delete my Vercel project",
]
SHOULD_HIT = ["repeat my last swiggy order please", "Repeat my last Swiggy order."]


@pytest.fixture(scope="module")
def sims():
    import asyncio

    from app.memory.embeddings import FastEmbedEmbedder

    emb = FastEmbedEmbedder(Settings().embedding_model)
    texts = [ANCHOR, *MUST_MISS, *SHOULD_HIT]
    vecs = asyncio.run(emb.embed([normalise_prompt(t) for t in texts]))
    return dict(zip(texts[1:], (vecs[1:] @ vecs[0]).tolist()))


def test_different_intents_stay_below_threshold(sims):
    t = Settings().semantic_cache_threshold
    leaks = {k: round(v, 3) for k, v in sims.items() if k in MUST_MISS and v >= t}
    assert not leaks, f"different intents would be served from cache at threshold {t}: {leaks}"


def test_near_verbatim_rewording_still_hits(sims):
    t = Settings().semantic_cache_threshold
    assert all(sims[k] >= t for k in SHOULD_HIT), {k: round(sims[k], 3) for k in SHOULD_HIT}
