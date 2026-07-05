"""
rag/embed.py — turn log rows into embeddable text chunks, and embed them.

Chunking: one self-contained text blob per logged decision / trade, with
the timestamp, bot, signal and full plain-English reasoning inline — the
same wording the Reasoning Log shows, so an answer grounded in a chunk is
verifiable against the dashboard.

Embeddings: sentence-transformers with a small model (all-MiniLM-L6-v2) —
embeddings are cheap and don't need to be MLX-native. If a pure-MLX stack
ever matters more than simplicity, `mlx-embeddings` is a drop-in
alternative here (swap _model() and encode()); not building both.
"""

from __future__ import annotations

import os

import numpy as np

from . import MLX_AVAILABLE

DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_MODEL_NAME = os.environ.get("TRADEBOT_EMBED_MODEL", DEFAULT_EMBED_MODEL)

_model = None


def _ensure_loaded():
    """Lazy singleton, same pattern as llm.py."""
    global _model
    if _model is not None:
        return
    if not MLX_AVAILABLE:
        raise RuntimeError("embedding stack not available on this machine")
    from sentence_transformers import SentenceTransformer
    print(f"[rag] loading embedder {EMBED_MODEL_NAME}...", flush=True)
    _model = SentenceTransformer(EMBED_MODEL_NAME)
    print("[rag] embedder ready", flush=True)


def encode(texts: list[str]) -> np.ndarray:
    """Embed texts -> float32 array (n, dim), L2-normalized so cosine
    similarity is a plain dot product."""
    _ensure_loaded()
    vecs = _model.encode(texts, normalize_embeddings=True,
                         show_progress_bar=False)
    return np.asarray(vecs, dtype=np.float32)


# ---------------------------------------------------------------------------
# Chunk builders — one text blob per source row
# ---------------------------------------------------------------------------

def _ts(iso: str | None) -> str:
    """'2026-07-02T14:31:09.123+00:00' -> '2026-07-02 14:31 UTC'."""
    if not iso:
        return "unknown time"
    return iso[:16].replace("T", " ") + " UTC"


def decision_chunk(bot: str, row: dict) -> str:
    """A Reasoning Log entry as one embeddable blob."""
    acted = "ACTED" if row.get("acted") else "passed"
    conf = (f", confidence {row['confidence']:.2f}"
            if row.get("confidence") is not None else "")
    return (f"[{_ts(row.get('ts'))}] {bot} — decision {row.get('signal')} "
            f"({acted}{conf}) — market: \"{row.get('market_question') or 'unknown'}\"\n"
            f"{row.get('reasoning') or ''}")


def trade_chunk(bot: str, row: dict) -> str:
    """A Trade History entry (fill) as one embeddable blob."""
    src = "manual trade by a human" if row.get("source") == "manual" else "agent trade"
    pnl = ""
    if row.get("action") == "SELL" and row.get("entry_avg") is not None:
        realized = (row["price"] - row["entry_avg"]) * row["shares"]
        pnl = f", realized P&L ${realized:+.2f} (entry {row['entry_avg']:.3f})"
    return (f"[{_ts(row.get('executed_at'))}] {bot} — {src}: {row.get('action')} "
            f"{row.get('side')} {row.get('shares'):.2f} shares @ {row.get('price'):.3f} "
            f"(${row.get('total_cost'):.2f}{pnl}) — market: "
            f"\"{row.get('market_question') or 'unknown'}\"\n"
            f"{row.get('reasoning') or ''}")
