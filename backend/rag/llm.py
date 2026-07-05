"""
rag/llm.py — thin wrapper around mlx-lm: load the model once, generate.

The generation model is a small instruction-tuned LLM from the
mlx-community org on Hugging Face, 4-bit quantized — loads in seconds and
answers in real time, which is plenty for summarizing a few retrieved log
lines. Swap it with TRADEBOT_LLM_MODEL.

The model is loaded lazily into a module-level singleton on FIRST request
(not at server boot — people who never open the chat tab shouldn't pay the
load), and exactly once per process. Generation is serialized with a lock:
the dashboard is multi-threaded but the Metal-backed model is not.
"""

from __future__ import annotations

import os
import threading

from . import MLX_AVAILABLE

# Qwen answered temporal questions ("what did you open today?") that
# Llama-3.2-3B-Instruct refused despite identical context; both are ~1.8GB
# 4-bit. Swap via TRADEBOT_LLM_MODEL if you prefer another mlx-community model.
DEFAULT_MODEL = "mlx-community/Qwen2.5-3B-Instruct-4bit"
MODEL_NAME = os.environ.get("TRADEBOT_LLM_MODEL", DEFAULT_MODEL)
MAX_ANSWER_TOKENS = int(os.environ.get("TRADEBOT_LLM_MAX_TOKENS", "400"))

_lock = threading.Lock()
_model = None
_tokenizer = None


def is_loaded() -> bool:
    return _model is not None


def _ensure_loaded():
    """Load the model into the process-wide singleton (once)."""
    global _model, _tokenizer
    if _model is not None:
        return
    if not MLX_AVAILABLE:
        raise RuntimeError("MLX is not available on this machine")
    from mlx_lm import load
    print(f"[rag] loading LLM {MODEL_NAME} (first question only)...", flush=True)
    _model, _tokenizer = load(MODEL_NAME)
    print("[rag] LLM ready", flush=True)


def generate_answer(system: str, user: str) -> str:
    """One-shot chat completion. Serialized — one generation at a time."""
    with _lock:
        _ensure_loaded()
        from mlx_lm import generate
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        prompt = _tokenizer.apply_chat_template(messages, add_generation_prompt=True)
        return generate(_model, _tokenizer, prompt=prompt,
                        max_tokens=MAX_ANSWER_TOKENS).strip()


if __name__ == "__main__":
    # Build-order step 1: prove the model loads and generates, in isolation.
    #   ~/.polymarket-paper/venv/bin/python backend/rag/llm.py "your prompt"
    import sys
    q = " ".join(sys.argv[1:]) or "In one sentence, what is a prediction market?"
    print(generate_answer("You are a concise assistant.", q))
