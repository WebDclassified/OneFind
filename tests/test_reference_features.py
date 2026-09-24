"""Keep the canonical feature reference synchronized with the project."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "REFERENCE_FEATURES.md"


def test_feature_reference_has_stable_complete_ids():
    text = REFERENCE.read_text(encoding="utf-8")
    ids = re.findall(r"\| (F-\d{2}) \|", text)
    assert ids == [f"F-{index:02d}" for index in range(1, 23)]
    assert "## Change rule" in text
    assert "sshot/manifest.json" in text
    assert "showcase-data/" in text


def test_feature_reference_targets_exist():
    text = REFERENCE.read_text(encoding="utf-8")
    referenced = set(re.findall(r"`((?:src|tests|benchmarks|docs|tools|sshot|sample-data|showcase-data)/[^`]+)`", text))
    missing = [path for path in referenced if "*" not in path and not (ROOT / path).exists()]
    assert not missing, missing


def test_presentation_and_showcase_docs_exist():
    for relative in (
        "docs/08-presentation-guide.md",
        "docs/09-showcase-corpus.md",
        "tools/build_showcase_corpus.py",
        "tools/browser_check.py",
        "sshot/manifest.json",
    ):
        assert (ROOT / relative).is_file()
