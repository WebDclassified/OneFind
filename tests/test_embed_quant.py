"""T-04/T-05 quantizer math - pure numpy, no model download needed."""

import numpy as np
import pytest

from onefind.embed import (
    calibrate_scale,
    compose_embed_text,
    pack_bits,
    quantize_int8,
    serialize_float32,
)


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def test_calibrate_scale_is_absmax_over_127():
    vecs = np.array([[1.0, -0.5], [0.25, 2.54]], dtype=np.float32)
    assert calibrate_scale(vecs) == pytest.approx(2.54 / 127.0)


def test_quantize_int8_preserves_cosine_direction():
    rng = np.random.default_rng(7)
    a = rng.normal(size=384).astype(np.float32)
    b = rng.normal(size=384).astype(np.float32)
    scale = calibrate_scale(np.stack([a, b]))

    qa = np.frombuffer(quantize_int8(a, scale), dtype=np.int8).astype(np.float32)
    qb = np.frombuffer(quantize_int8(b, scale), dtype=np.int8).astype(np.float32)

    assert _cosine(qa, qb) == pytest.approx(_cosine(a, b), abs=5e-3)
    assert qa.max() <= 127 and qa.min() >= -127


def test_pack_bits_sign_convention_and_packing():
    v = np.array([0.5, -0.1, 0.3, -0.7], dtype=np.float32)
    packed = pack_bits(v)  # bits: 1,0,1,0 then 4 zero pad -> 1010 0000
    assert packed == b"\xa0"


def test_serialize_float32_roundtrip():
    v = np.array([1.0, -2.5, 0.25], dtype=np.float32)
    blob = serialize_float32(v)
    assert len(blob) == 12
    assert np.frombuffer(blob, dtype="<f4").tolist() == [1.0, -2.5, 0.25]


def test_compose_embed_text_joins_title_body():
    assert compose_embed_text("Title", "Body text") == "Title\nBody text"
    assert compose_embed_text("", "Just body") == "Just body"
    assert compose_embed_text("  ", "   ") == ""
