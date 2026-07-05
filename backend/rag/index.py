"""
rag/index.py — build/refresh the per-portfolio vector index, cached in the
same SQLite file as everything else (table rag_chunks, created by
engine._ensure_extra_schema).

Indexing is INCREMENTAL: rag_chunks remembers the highest source row id
embedded per (portfolio, source_type), and each refresh only embeds
decisions/trades newer than that — restarting the server never re-embeds
history. Indexing runs lazily when a question arrives (the first question
backfills the whole log; the "waking up" state in the UI covers it) rather
than on every agent cycle, so the embedding model only ever loads in a
process where someone actually uses the chat.

Retrieval: the portfolio's embeddings are loaded into a numpy array and
scored by cosine similarity (vectors are L2-normalized, so it's a dot
product). At this project's realistic corpus size — thousands of rows, not
millions — a vector database would be pure overhead.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine  # noqa: E402

from . import embed  # noqa: E402

_EMBED_BATCH = 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _last_indexed(conn, name: str, source_type: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(source_id), 0) m FROM rag_chunks "
        "WHERE portfolio_name = ? AND source_type = ?", (name, source_type),
    ).fetchone()
    return row["m"] if row else 0


def _new_decision_chunks(conn, name: str, bot: str) -> list[tuple]:
    since = _last_indexed(conn, name, "decision")
    rows = conn.execute(
        "SELECT * FROM decisions WHERE portfolio_name = ? AND id > ? ORDER BY id",
        (name, since),
    ).fetchall()
    return [("decision", r["id"], r["ts"], embed.decision_chunk(bot, dict(r)))
            for r in rows]


def _new_trade_chunks(conn, name: str, bot: str) -> list[tuple]:
    pf = conn.execute(
        "SELECT id FROM portfolios WHERE name = ? AND active = 1 "
        "ORDER BY id DESC LIMIT 1", (name,)).fetchone()
    if not pf:
        return []
    since = _last_indexed(conn, name, "trade")
    rows = conn.execute(
        "SELECT * FROM trades WHERE portfolio_id = ? AND id > ? ORDER BY id",
        (pf["id"], since),
    ).fetchall()
    return [("trade", r["id"], r["executed_at"], embed.trade_chunk(bot, dict(r)))
            for r in rows]


def refresh(name: str) -> int:
    """Embed anything logged since the last refresh. Returns rows added."""
    bot = engine.bot_identity(name)
    conn = engine._conn()
    try:
        pending = (_new_decision_chunks(conn, name, bot)
                   + _new_trade_chunks(conn, name, bot))
        if not pending:
            return 0
        for i in range(0, len(pending), _EMBED_BATCH):
            batch = pending[i:i + _EMBED_BATCH]
            vecs = embed.encode([text for *_ignored, text in batch])
            conn.executemany(
                """INSERT OR IGNORE INTO rag_chunks
                     (portfolio_name, source_type, source_id, source_ts,
                      text, embedding, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [(name, st, sid, sts, text, vec.tobytes(), _now())
                 for (st, sid, sts, text), vec in zip(batch, vecs)],
            )
            conn.commit()
        return len(pending)
    finally:
        conn.close()


def search(name: str, query: str, top_k: int = 6, min_score: float = 0.18,
           recent_k: int = 3) -> list[dict]:
    """Retrieve chunks for a question: top-k by cosine similarity above a
    floor, PLUS the most recent few entries regardless of score.

    The recency blend is what makes "what did you open today?" answerable —
    such questions share almost no vocabulary with individual log lines, so
    pure similarity misses them. Off-topic questions are still refused, by
    the model's grounding rules rather than the retrieval floor (a floor
    alone can't tell "bitcoin last month" from a weakly-matching World Cup
    line anyway). Returns [{text, timestamp, source_type, score}]; empty
    only when the portfolio has no indexed log at all."""
    conn = engine._conn()
    try:
        rows = conn.execute(
            "SELECT id, source_type, source_ts, text, embedding FROM rag_chunks "
            "WHERE portfolio_name = ?", (name,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return []
    mat = np.frombuffer(b"".join(r["embedding"] for r in rows),
                        dtype=np.float32).reshape(len(rows), -1)
    q = embed.encode([query])[0]
    scores = mat @ q  # normalized vectors: dot product == cosine similarity

    picked: list[int] = [i for i in np.argsort(scores)[::-1][:top_k]
                         if scores[i] >= min_score]
    newest = sorted(range(len(rows)), key=lambda i: rows[i]["source_ts"] or "",
                    reverse=True)[:recent_k]
    picked += [i for i in newest if i not in picked]

    return [{
        "text": rows[i]["text"],
        "timestamp": rows[i]["source_ts"],
        "source_type": rows[i]["source_type"],
        "score": round(float(scores[i]), 3),
    } for i in picked]


if __name__ == "__main__":
    # Build-order step 2: index a portfolio and inspect what got embedded.
    #   venv/bin/python backend/rag/index.py <portfolio> ["query"]
    pf = sys.argv[1] if len(sys.argv) > 1 else engine.resolve_profile(None)
    added = refresh(pf)
    print(f"indexed {added} new chunks for {pf}")
    if len(sys.argv) > 2:
        for hit in search(pf, sys.argv[2]):
            print(f"\n--- {hit['score']} · {hit['source_type']} · {hit['timestamp']}")
            print(hit["text"][:300])
