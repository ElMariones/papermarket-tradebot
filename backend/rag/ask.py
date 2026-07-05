"""
rag/ask.py — retrieval + grounded prompt assembly + answer.

Grounding is the part that matters most: the biggest risk is the model
confidently inventing a trade that never happened. Three guards:

  1. The system prompt orders the model to use ONLY the retrieved log
     excerpts, and to say plainly when they don't cover the question —
     never to fill gaps from general knowledge about Polymarket or trading.
  2. The retrieved chunks are returned alongside the answer so the UI can
     show the receipts under every reply.
  3. If the bot has no indexed log at all (fresh portfolio), the model is
     never called — "no matching log entries" is cheaper and more honest
     than asking a model to answer from nothing. (Off-topic questions
     against a non-empty log are refused by rule 1+2 instead: retrieval
     blends in recent entries by design, see index.search.)

Read-only by construction: this module can SELECT from the log tables and
INSERT into rag_chunks, and nothing else. No question, however phrased,
can reach a code path that trades or controls an agent.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from . import MLX_AVAILABLE, UNAVAILABLE_REASON  # noqa: E402
from . import index, llm  # noqa: E402

TOP_K = 6
# Retrieval floor. Questions and log lines are asymmetric text ("what did
# you open today?" vs "BUY NO 4.78 shares @ 0.837"), so MiniLM cosine scores
# run low — 0.18 keeps legitimate questions retrieving while still cutting
# true noise. The model's own refusal rule handles off-topic retrievals.
MIN_SCORE = 0.18

NO_MATCH_ANSWER = ("I don't see anything in this bot's log about that — "
                   "no matching log entries found.")

SYSTEM_PROMPT = """\
You are the trading log assistant for one paper-trading bot on a Polymarket \
dashboard. You answer questions about THIS bot's own logged activity.

Rules, in order of importance:
1. Answer ONLY from the log excerpts provided in the user message. They are \
the complete evidence available to you.
2. If the excerpts do not mention the topic of the question at all, reply \
exactly: "I don't see anything in the log about that." Do not guess, do not \
answer from general knowledge about Polymarket, prediction markets, or trading.
3. If the excerpts DO mention the topic but don't fully explain it, report \
what the log shows (actions, prices, sizes, timestamps) and say what it \
doesn't record. Example: a "manual trade by a human" was a person's own \
decision from the Markets page — the log records the fill but no strategy \
reasoning.
4. Never invent trades, markets, prices, or dates that are not in the excerpts.
5. You are read-only. You cannot start, stop, or trade anything; if asked to, \
say you can only explain the log.
6. Be concise and concrete: quote the relevant reasoning, prices, and \
timestamps from the excerpts.

Vocabulary of the log: a BUY opens a position, a SELL closes one. \
"decision ... (ACTED)" means the bot traded; "(passed)" means it looked and \
declined. "manual trade by a human" is the owner trading by hand, not the \
bot's strategy."""


def status() -> dict:
    """For GET /api/mlx-status."""
    return {
        "available": MLX_AVAILABLE,
        "model": llm.MODEL_NAME if MLX_AVAILABLE else None,
        "loaded": llm.is_loaded() if MLX_AVAILABLE else False,
        "reason": None if MLX_AVAILABLE else
                  "MLX (Apple-Silicon-only) or its dependencies are not "
                  "installed on this deployment.",
    }


def ask(portfolio_name: str, question: str) -> dict:
    """Answer a question about one portfolio, grounded in its own log.
    Returns {answer, sources: [{text, timestamp, source_type, score}]}."""
    if not MLX_AVAILABLE:
        raise RuntimeError(
            f"Ask the Bot is unavailable here ({UNAVAILABLE_REASON})")
    question = (question or "").strip()
    if not question:
        raise ValueError("Empty question")
    if len(question) > 500:
        raise ValueError("Question too long (500 chars max)")

    index.refresh(portfolio_name)  # incremental — cheap after first call
    sources = index.search(portfolio_name, question,
                           top_k=TOP_K, min_score=MIN_SCORE)
    if not sources:
        return {"answer": NO_MATCH_ANSWER, "sources": []}

    excerpts = "\n\n".join(
        f"--- log excerpt {i+1} ({s['source_type']}) ---\n{s['text']}"
        for i, s in enumerate(sources))
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # Small models weight the end of the prompt heaviest, so the grounding
    # instruction is restated right after the question. The current time is
    # included so "today"/"recently" in questions can be resolved against
    # the excerpt timestamps.
    user_msg = (f"Current time: {now}\n\n"
                f"Log excerpts from this bot's own records:\n\n{excerpts}\n\n"
                f"Question: {question}\n\n"
                "Answer using only the log excerpts above. If they mention "
                "the topic, summarize exactly what happened (actions, "
                "prices, times, reasoning when recorded). Only if they don't "
                "mention it at all, say you don't see it in the log.")
    answer = llm.generate_answer(SYSTEM_PROMPT, user_msg)
    return {"answer": answer, "sources": sources}


if __name__ == "__main__":
    # Build-order step 3: grounded Q&A from the CLI.
    #   venv/bin/python backend/rag/ask.py <portfolio> "why did you buy X?"
    import json
    pf = sys.argv[1]
    q = " ".join(sys.argv[2:]) or "what did you trade most recently, and why?"
    out = ask(pf, q)
    print("ANSWER:\n" + out["answer"])
    print("\nSOURCES:")
    for s in out["sources"]:
        print(f"  [{s['score']}] {s['source_type']} {s['timestamp']}: "
              + s["text"].replace("\n", " ")[:120])
