"""Bounded local CPU embedding adapter. Model loading is lazy and failure is safe."""

from __future__ import annotations

import os
from pathlib import Path
from threading import Lock

from app.storage.local_file_security import secure_private_directory

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_VERSION = "fastembed-0.9.1"
_lock = Lock()
_model = None


def _cache_dir() -> Path:
    configured = os.environ.get("TRENDSCOUT_MODEL_CACHE")
    if configured:
        path = Path(configured)
    elif os.name == "nt":
        app_data = os.environ.get("LOCALAPPDATA")
        if not app_data:
            raise RuntimeError("LOCALAPPDATA is required for the Windows model cache")
        path = Path(app_data) / "TrendScout" / "models" / "fastembed"
    else:
        path = Path.home() / ".cache" / "trendscout" / "fastembed"
    secure_private_directory(path)
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
