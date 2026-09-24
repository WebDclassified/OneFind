"""Embedding pipeline (Phases 2): model wrapper + precision transforms.

Heavy deps (sentence-transformers/torch/numpy) are imported lazily so the
lexical-only install stays light. Quantizer helpers are plain-numpy so they
are unit-testable without downloading any model.

Precision conventions (mirroring arXiv:2608.24060 configurations):
- float : sqlite-vec cosine KNN over float32 vectors
- int8  : global-scale scalar quantization, application-side cosine
- binary: sign-bit Hamming distance, application-side
"""

from __future__ import annotations

import numpy as np

# single source of truth for the default model lives in envcheck
from .envcheck import DEFAULT_MODEL  # noqa: F401 - re-exported

__all__ = ["DEFAULT_MODEL", "SentenceEmbedder"]


class SentenceEmbedder:
    """Wraps sentence-transformers; emits unit-normalized float32 vectors."""

    def __init__(self, model_name: str) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise ImportError(
                "sentence-transformers not installed - run: pip install -e \".[model]\""
            ) from exc
        self.name = model_name
        self.model = SentenceTransformer(model_name, device="cpu")
        model_card = getattr(self.model, "model_card_data", None)
        self.revision = getattr(model_card, "commit_hash", None) or "unpinned"
        get_dim = getattr(self.model, "get_embedding_dimension", None)
        self.dimension = int(
            get_dim() if get_dim is not None else self.model.get_sentence_embedding_dimension()
        )

    def _encode(self, texts: list[str]) -> np.ndarray:
        return self.model.encode(
            texts,
            batch_size=64,
            convert_to_numpy=True,
            normalize_embeddings=True,
        ).astype(np.float32)

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        return self._encode(texts)

    def encode_query(self, text: str) -> np.ndarray:
        return self._encode([text])[0]


def compose_embed_text(title: str, body: str) -> str:
    """BEIR-style document text: title and body joined by newline."""
    parts = [p.strip() for p in (title, body) if p and p.strip()]
    return "\n".join(parts)


def calibrate_scale(vectors: np.ndarray) -> float:
    """Global symmetric int8 scale from a calibration batch (absmax/127)."""
    absmax = float(np.abs(vectors).max()) if vectors.size else 0.0
    return absmax / 127.0 if absmax > 0.0 else 1e-8


def quantize_int8(vector: np.ndarray, scale: float) -> bytes:
    """Quantize with a shared global scale; direction (hence cosine) survives."""
    q = np.clip(np.round(vector / scale), -127, 127).astype(np.int8)
    return q.tobytes()


def pack_bits(vector: np.ndarray) -> bytes:
    """Sign-bit binary quantization: component > 0 -> 1, row-major packed."""
    bits = (vector > 0).astype(np.uint8)
    pad = (-bits.size) % 8
    if pad:
        bits = np.concatenate([bits, np.zeros(pad, dtype=np.uint8)])
    return np.packbits(bits).tobytes()


def serialize_float32(vector: np.ndarray) -> bytes:
    """Compact little-endian float32 buffer accepted by sqlite-vec."""
    return np.asarray(vector, dtype="<f4").tobytes()
