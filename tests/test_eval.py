"""T-07 acceptance: dataset loaders + end-to-end mini-BEIR evaluation."""

import json

import pytest

from onefind.datasets import load_corpus, load_qrels, load_queries
from onefind.errors import DataError


# ---- loaders (torch-free) ------------------------------------------------------


def _make_fixture(tmp_path, corpus=None, queries=None, qrels=None):
    corpus = corpus or [
        {"_id": "d1", "title": "Citrus", "text": "oranges vitamin C"},
        {"_id": "d2", "title": "Bread", "text": "sourdough yeast"},
    ]
    queries = queries or [
        {"_id": "q1", "text": "vitamin"},
        {"_id": "q2", "text": "yeast"},
    ]
    qrels = qrels or [("query-id", "corpus-id", "score"), ("q1", "d1", "1"), ("q2", "d2", "2")]
    (tmp_path / "corpus.jsonl").write_text(
        "\n".join(json.dumps(r) for r in corpus), encoding="utf-8"
    )
    (tmp_path / "queries.jsonl").write_text(
        "\n".join(json.dumps(r) for r in queries), encoding="utf-8"
    )
    (tmp_path / "qrels.tsv").write_text(
        "\n".join("\t".join(r) for r in qrels) + "\n", encoding="utf-8"
    )
    return tmp_path


def test_load_corpus_and_queries(tmp_path):
    folder = _make_fixture(tmp_path)
    corpus = load_corpus(folder)
    assert corpus["d1"]["title"] == "Citrus"
    assert load_queries(folder)["q2"] == "yeast"


def test_load_qrels_parses_header_and_scores(tmp_path):
    folder = _make_fixture(tmp_path)
    qrels = load_qrels(folder)
    assert qrels == {"q1": {"d1": 1}, "q2": {"d2": 2}}


def test_missing_files_raise_data_error(tmp_path):
    with pytest.raises(DataError):
        load_corpus(tmp_path)


# ---- end-to-end harness on a synthetic dataset ----------------------------------


@pytest.mark.parametrize("none", [None])
def test_run_eval_writes_report_with_all_configs(none, tmp_path):
    ranx = pytest.importorskip("ranx")
    sentence_transformers = pytest.importorskip("sentence_transformers")  # noqa: F841
    del none

    from onefind.embed import DEFAULT_MODEL, SentenceEmbedder
    from onefind.evaluate import CONFIGS, run_eval

    folder = _make_fixture(
        tmp_path,
        corpus=[
            {"_id": f"d{i}", "title": t, "text": x}
            for i, (t, x) in enumerate([
                ("Citrus", "oranges are citrus fruits rich in vitamin C"),
                ("Photosynthesis", "plants convert sunlight into glucose"),
                ("Sourdough", "wild yeast starter leavens bread dough"),
                ("Running", "foam midsoles cushion and return energy"),
            ])
        ],
        queries=[
            {"_id": "q1", "text": "vitamin C in oranges"},
            {"_id": "q2", "text": "how plants make food from sunlight"},
            {"_id": "q3", "text": "bread starter yeast"},
            {"_id": "q4", "text": "cushioning for runners"},
        ],
        qrels=[  # ids follow the enumerate() above: d0..d3
            ("query-id", "corpus-id", "score"),
            ("q1", "d0", "1"),
            ("q2", "d1", "1"),
            ("q3", "d2", "1"),
            ("q4", "d3", "1"),
        ],
    )
    db = tmp_path / "mini.db"

    def once(tag):
        report = run_eval(
            str(folder),
            db=db,
            k=4,
            model_name=DEFAULT_MODEL,
            out_dir=tmp_path / f"reports-{tag}",
        )
        return report.read_text(encoding="utf-8")

    first = once("a")
    for label_part in ("lexical", "semantic(float)", "hybrid(binary)", "+rerank"):
        assert label_part in first, label_part
    assert len(CONFIGS) == 8
    # sanity: lexical must be non-zero on this coherent fixture
    assert "| lexical | 0.0000" not in first

    second = once("b")
    # effectiveness must be deterministic run-to-run; drop latency columns
    def effectiveness(text):
        rows = []
        for line in text.splitlines():
            if line.startswith("| ") and "---" not in line and "Configuration" not in line:
                cells = [c.strip() for c in line.split("|")[1:-1]]
                rows.append(cells[:5])  # label + ndcg/ap/rr/precision
        return rows

    assert effectiveness(first) == effectiveness(second)
