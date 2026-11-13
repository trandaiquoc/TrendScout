"""Bounded local CPU embedding adapter. Model loading is lazy and failure is safe."""

from __future__ import annotations

import os
from pathlib import Path
from threading import Lock

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_VERSION = "fastembed-0.9.1"
_lock = Lock()
_model = None


def _cache_dir() -> Path:
    path = Path(os.environ.get("TRENDSCOUT_MODEL_CACHE", Path.home() / ".cache" / "trendscout" / "fastembed"))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def embed(texts: list[str]) -> list[list[float]]:
    """Return CPU vectors for at most 256 short strings; never download on requests."""
    global _model
    if not isinstance(texts, list) or len(texts) > 256 or any(
        not isinstance(text, str) or not text.strip() or len(text) > 1200 for text in texts
    ):
        raise ValueError("embedding input must contain at most 256 bounded strings")
    if not texts:
        return []
    with _lock:
        if _model is None:
            from fastembed import TextEmbedding

            _model = TextEmbedding(
                model_name=MODEL_NAME,
                cache_dir=str(_cache_dir()),
                threads=1,
                cuda=False,
                lazy_load=True,
            )
        return [vector.tolist() for vector in _model.embed(texts, batch_size=32, parallel=1)]


def available() -> bool:
    try:
        import fastembed  # noqa: F401
        return True
    except Exception:
        return False
