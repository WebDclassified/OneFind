"""v1.2 snippet helpers: passages, highlight segments, FTS marker parsing."""

import random

import pytest

from onefind.search import Hit
from onefind.snippets import (
    ELLIPSIS,
    FTS_END,
    FTS_START,
    MAX_HIGHLIGHTS,
    best_passage,
    build_snippet,
    collapse_whitespace,
    segments_from_fts,
    segments_with_terms,
    stem_variants,
)


def _highlighted(segments):
    return [segment["text"] for segment in segments if segment["highlight"]]


def _joined(segments):
    return "".join(segment["text"] for segment in segments)


def _core(passage):
    core = passage
    if core.startswith(ELLIPSIS + " "):
        core = core[2:]
    if core.endswith(" " + ELLIPSIS):
        core = core[:-2]
    return core


def _assert_word_aligned(passage, collapsed):
    core = _core(passage)
    position = collapsed.find(core)
    assert position >= 0, "passage must be verbatim (collapsed) document text"
    end = position + len(core)
    assert position == 0 or collapsed[position - 1] == " "
    assert end == len(collapsed) or collapsed[end] == " "
    assert passage.startswith(ELLIPSIS) == (position > 0)
    assert passage.endswith(ELLIPSIS) == (end < len(collapsed))


# ---- segments_from_fts -------------------------------------------------------


def test_fts_markers_become_segments():
    snippet = f"plants use {FTS_START}chlorophyll{FTS_END} to absorb {FTS_START}light{FTS_END}"
    assert segments_from_fts(snippet) == [
        {"text": "plants use ", "highlight": False},
        {"text": "chlorophyll", "highlight": True},
        {"text": " to absorb ", "highlight": False},
        {"text": "light", "highlight": True},
    ]


def test_fts_empty_and_unbalanced_markers():
    assert segments_from_fts("") == [{"text": "", "highlight": False}]
    assert segments_from_fts(None) == [{"text": "", "highlight": False}]
    assert segments_from_fts(f"a {FTS_START}open") == [
        {"text": "a ", "highlight": False},
        {"text": "open", "highlight": True},
    ]
    assert segments_from_fts(f"{FTS_END}tail") == [{"text": "tail", "highlight": False}]


def test_fts_segments_match_legacy_serve_helper():
    serve = pytest.importorskip("onefind.serve")
    for snippet in ("", "plain", f"x {FTS_START}y{FTS_END} z", f"{FTS_START}{FTS_END}"):
        assert segments_from_fts(snippet) == serve._segments_from_fts(snippet)


# ---- segments_with_terms -----------------------------------------------------


def test_whole_words_only_and_case_insensitive():
    segments = segments_with_terms("Cat, concatenate, CAT and cats.", "cat")
    assert _highlighted(segments) == ["Cat", "CAT", "cats"]
    assert _joined(segments) == "Cat, concatenate, CAT and cats."


@pytest.mark.parametrize(
    ("query", "text", "expected"),
    [
        ("running", "run runs runner running rune ran", ["run", "runs", "runner", "running"]),
        ("studies", "study studied studying student", ["study", "studied", "studying"]),
        ("notes", "note noted notes nothing", ["note", "noted", "notes"]),
        ("renew", "Renewals are renewed; the renewal fee", ["Renewals", "renewed", "renewal"]),
        ("boxes", "a box, two boxes", ["box", "boxes"]),
        ("class", "classes in class, classic", ["classes", "class", "classic"]),
    ],
)
def test_simple_stems_and_prefixes(query, text, expected):
    assert _highlighted(segments_with_terms(text, query)) == expected


def test_short_terms_do_not_prefix_match():
    # "run" is shorter than the 4-char prefix minimum: "rune" stays plain.
    assert _highlighted(segments_with_terms("run rune runway", "run")) == ["run"]


def test_unicode_case_folding_and_diacritics():
    assert _highlighted(segments_with_terms("Visit the Café today", "cafe")) == ["Café"]
    assert _highlighted(segments_with_terms("Die STRASSE ist lang", "straße")) == ["STRASSE"]
    assert _highlighted(segments_with_terms("I don’t know", "don't")) == ["don’t"]
    assert _highlighted(segments_with_terms("Ελληνικά κείμενα", "ΕΛΛΗΝΙΚΆ")) == ["Ελληνικά"]


def test_single_character_and_punctuation_queries_highlight_nothing():
    assert _highlighted(segments_with_terms("a b c", "a")) == []
    assert segments_with_terms("some text", '"" () :') == [{"text": "some text", "highlight": False}]


def test_empty_text_and_whitespace_collapse():
    assert segments_with_terms("", "q") == []
    assert segments_with_terms("   \n\t ", "q") == []
    segments = segments_with_terms("alpha\n\n  beta\tgamma", "beta")
    assert _joined(segments) == "alpha beta gamma"
    assert _highlighted(segments) == ["beta"]


def test_highlights_are_capped():
    text = " ".join(["orbit"] * (MAX_HIGHLIGHTS + 50))
    segments = segments_with_terms(text, "orbit", max_chars=10_000)
    assert len(_highlighted(segments)) == MAX_HIGHLIGHTS
    assert _joined(segments) == text


def test_long_text_is_clipped_at_a_word_boundary():
    text = "word " * 400
    segments = segments_with_terms(text, "word", max_chars=103)
    joined = _joined(segments)
    assert joined.endswith(" " + ELLIPSIS)
    body = joined[: -len(" " + ELLIPSIS)]
    assert len(body) <= 103
    assert all(token == "word" for token in body.split(" "))


# ---- stems -------------------------------------------------------------------


def test_stem_variants_are_deterministic_and_bounded():
    assert stem_variants("running") == frozenset({"run"})
    assert stem_variants("noted") == frozenset({"not", "note"})
    assert stem_variants("status") == frozenset({"status"})
    assert stem_variants("used") == frozenset({"used"})  # stem would be too short
    assert stem_variants("Owner's") == stem_variants("owner")  # possessive dropped
    assert all(len(variant) >= 3 for variant in stem_variants("filling"))


# ---- best_passage ------------------------------------------------------------


LONG_BODY = (
    "# Release handbook\n\n"
    "This handbook explains how releases move from a branch to production. "
    "Every release starts with a freeze window and a written plan. "
    "Teams publish the plan in the shared channel before the freeze begins.\n\n"
    "Testing happens in a staging environment that mirrors production closely. "
    "Load tests run nightly and their reports are archived for a quarter. "
    "Flaky tests are quarantined instead of silently retried.\n\n"
    "If a deploy misbehaves, the on-call engineer performs a rollback and names an owner "
    "for the follow-up review. The rollback owner writes the incident summary within two days. "
    "Summaries are linked from the release calendar.\n\n"
    "Finally, retired feature flags are deleted once the release is stable for a week."
)


def test_short_body_is_returned_whole():
    body = "Line one.\n\nLine   two."
    assert best_passage(body, "two", max_chars=320) == "Line one. Line two."


def test_passage_finds_the_relevant_region():
    passage = best_passage(LONG_BODY, "rollback owner", max_chars=200)
    assert "rollback" in passage and "owner" in passage
    assert passage.startswith(ELLIPSIS + " ")
    assert len(_core(passage)) <= 200
    _assert_word_aligned(passage, " ".join(_units(LONG_BODY)))


def _units(body):
    from onefind.snippets import _split_units

    return _split_units(body)


def test_passage_prefers_more_distinct_terms():
    filler = " ".join(f"filler{n}." for n in range(80))
    body = f"Alpha alpha alpha alpha. {filler} The alpha and beta pair appears here. {filler}"
    passage = best_passage(body, "alpha beta", max_chars=120)
    assert "beta" in passage


def test_passage_without_matches_starts_at_beginning():
    passage = best_passage(LONG_BODY, "zebra", max_chars=150)
    assert not passage.startswith(ELLIPSIS)
    assert passage.startswith("Release handbook")  # heading marker stripped
    assert passage.endswith(ELLIPSIS)
    assert len(_core(passage)) <= 150


def test_passage_never_cuts_inside_words():
    passage = best_passage(LONG_BODY, "quarantined flaky", max_chars=90)
    collapsed = " ".join(_units(LONG_BODY))
    _assert_word_aligned(passage, collapsed)
    assert "Flaky" in passage or "quarantined" in passage


def test_single_giant_token_is_hard_cut_within_budget():
    body = "x" * 5000
    passage = best_passage(body, "x", max_chars=100)
    assert len(_core(passage)) <= 100
    assert passage.endswith(ELLIPSIS)


def test_tiny_budget_is_clamped():
    passage = best_passage(LONG_BODY, "rollback", max_chars=1)
    assert 0 < len(_core(passage)) <= 40


def test_empty_and_decoration_only_bodies():
    assert best_passage("", "q") == ""
    assert best_passage("---\n```\n| --- |\n", "q") == ""
    assert best_passage(None, "q") == ""


def test_huge_body_is_bounded():
    body = ("lorem ipsum dolor sit amet. " * 20_000) + "needle at the very end."
    passage = best_passage(body, "needle", max_chars=200)
    assert len(_core(passage)) <= 200  # scanning stops at the safety cap


def test_randomized_passage_invariants():
    rng = random.Random(1234)
    vocabulary = ["orbit", "water", "plan", "owner", "rollback", "a", "the", "notes", "running"]
    for _ in range(60):
        sentences = []
        for _ in range(rng.randint(1, 25)):
            words = [rng.choice(vocabulary) for _ in range(rng.randint(1, 18))]
            sentences.append(" ".join(words).capitalize() + rng.choice([".", "!", "?", ""]))
        body = rng.choice([" ", "\n", "\n\n"]).join(sentences)
        query = " ".join(rng.sample(vocabulary, rng.randint(1, 3)))
        limit = rng.randint(40, 400)
        passage = best_passage(body, query, max_chars=limit)
        collapsed = collapse_whitespace(body)
        assert len(_core(passage)) <= limit
        _assert_word_aligned(passage, collapsed)


# ---- build_snippet -----------------------------------------------------------


def test_build_snippet_prefers_body_passage():
    hit = Hit("d", "Doc", 1.0, f"x {FTS_START}rollback{FTS_END} y")
    segments = build_snippet(hit, LONG_BODY, "rollback owner")
    assert "rollback" in _highlighted(segments)
    assert "owner" in _highlighted(segments)
    assert all(FTS_START not in segment["text"] for segment in segments)


def test_build_snippet_falls_back_to_fts_markers():
    hit = Hit("d", "Doc", 1.0, f"the {FTS_START}geese{FTS_END} flew")
    # Porter matches goose/geese; the light stemmer does not, so FTS wins.
    segments = build_snippet(hit, "The geese flew south.", "goose")
    assert _highlighted(segments) == ["geese"]


def test_build_snippet_without_body_uses_fts_or_nothing():
    hit = {"snippet": f"{FTS_START}orbit{FTS_END} path"}
    assert _highlighted(build_snippet(hit, None, "orbit")) == ["orbit"]
    assert build_snippet(Hit("d", "t", 0.5, ""), "", "orbit") == []
    assert build_snippet(object(), None, "orbit") == []


def test_build_snippet_semantic_hit_without_matches_is_plain_passage():
    segments = build_snippet(Hit("d", "t", 0.5, ""), LONG_BODY, "zebra", max_chars=120)
    assert segments and not any(segment["highlight"] for segment in segments)
    assert len(_core(_joined(segments))) <= 120


def test_build_snippet_keeps_untrusted_markup_as_text():
    body = '<img src=x onerror="alert(1)"> trigger <script>alert(2)</script>'
    segments = build_snippet(Hit("u", "t", 1.0, ""), body, "trigger")
    assert "<img" in _joined(segments) and "<script>" in _joined(segments)
    assert _highlighted(segments) == ["trigger"]


# ---- stopwords -----------------------------------------------------------------


def test_stopwords_are_not_highlighted():
    segments = segments_with_terms("The orbit of the moon is what it is.", "what is the orbit")
    assert _highlighted(segments) == ["orbit"]
    assert _joined(segments) == "The orbit of the moon is what it is."
    contraction = segments_with_terms("What's the orbit? Don't ask.", "what's the orbit, don't")
    assert _highlighted(contraction) == ["orbit"]


def test_stopword_only_query_still_highlights():
    segments = segments_with_terms("The end of the list.", "the of")
    assert _highlighted(segments) == ["The", "of", "the"]


def test_passage_scores_content_words_not_stopwords():
    filler = " ".join(
        f"How the team does the work and what the plan is for week {n}." for n in range(12)
    )
    body = f"{filler} Orbit insertion burns happen near the planet."
    passage = best_passage(body, "how does the orbit insertion work", max_chars=120)
    assert "Orbit insertion" in passage
    # Without content words, stopword-only queries still pick a matching window.
    assert best_passage("x " * 200 + "the end", "the", max_chars=60).startswith(ELLIPSIS)


def test_build_snippet_fts_fallback_unmarks_stopwords():
    snippet = (f"{FTS_START}The{FTS_END} {FTS_START}orbit{FTS_END} of "
               f"{FTS_START}the{FTS_END} moon")
    hit = Hit("d", "t", 1.0, snippet)
    segments = build_snippet(hit, None, "the orbit")
    assert _highlighted(segments) == ["orbit"]
    assert _joined(segments) == "The orbit of the moon"
    assert segments == [
        {"text": "The ", "highlight": False},
        {"text": "orbit", "highlight": True},
        {"text": " of the moon", "highlight": False},
    ]
    only_stop = build_snippet(Hit("d", "t", 1.0, f"{FTS_START}The{FTS_END} end"), None, "the")
    assert _highlighted(only_stop) == ["The"]


def test_build_snippet_body_passage_ignores_stopwords():
    hit = Hit("d", "t", 1.0, "")
    segments = build_snippet(hit, "What is the orbit? The orbit is a path.", "what is the orbit")
    assert _highlighted(segments) == ["orbit", "orbit"]


# ---- title-only FTS matches ----------------------------------------------------------


PLAN_BODY = (
    "Volunteers meet every Saturday at the allotment gate. "
    "Bring gloves; tools are provided. The compost bays are emptied in spring."
)


def test_title_only_fts_match_shows_body_context_not_the_title():
    title = "Community Allotment Work Plan"
    hit = Hit("notes/plan.md", title, 1.0, f"Community Allotment Work {FTS_START}Plan{FTS_END}")
    segments = build_snippet(hit, PLAN_BODY, "plan")
    text = _joined(segments)
    assert segments and text != title
    assert text.startswith("Volunteers meet every Saturday")
    assert not any(segment["highlight"] for segment in segments)
    # Dict hits (and case / whitespace differences) behave the same way.
    as_dict = {"title": "community  allotment work plan",
               "snippet": f"Community {FTS_START}Allotment{FTS_END} Work Plan"}
    assert _joined(build_snippet(as_dict, PLAN_BODY, "plan")) == text


def test_title_only_fts_match_without_body_still_uses_the_fts_snippet():
    hit = Hit("notes/plan.md", "Work Plan", 1.0, f"Work {FTS_START}Plan{FTS_END}")
    assert _highlighted(build_snippet(hit, None, "plan")) == ["Plan"]


def test_body_fts_fallback_is_kept_when_it_is_not_the_title():
    # Porter matches goose/geese in the body; the title is different.
    hit = Hit("d", "Birds", 1.0, f"the {FTS_START}geese{FTS_END} flew")
    assert _highlighted(build_snippet(hit, "The geese flew south.", "goose")) == ["geese"]


# ---- Markdown cleanup ------------------------------------------------------------------


def test_table_rows_become_label_value_text():
    body = (
        "## Control facts\n\n"
        "| Field | Value |\n"
        "|---|---|\n"
        "| Record owner | Data Steward |\n"
        "| Escalation threshold | 82 % complete |\n"
        "| Related records | `DATA-002`, `DATA-003` |\n"
    )
    passage = best_passage(body, "escalation threshold")
    assert "|" not in passage and "`" not in passage
    assert "Record owner: Data Steward." in passage
    assert "Escalation threshold: 82 % complete." in passage
    assert "Related records: DATA-002, DATA-003." in passage
    assert passage.startswith("Control facts: Field: Value.")


def test_list_items_bold_and_code_are_plain_text():
    body = (
        "- **Status:** Approved\n"
        "- **Version:** `1.0.1`\n"
        "* [x] Signed off by [the board](https://example.org/board)\n"
        "> quoted __important note__ here\n"
    )
    passage = best_passage(body, "status")
    assert passage == (
        "Status: Approved. Version: 1.0.1. Signed off by the board. "
        "quoted important note here"
    )


def test_heading_reads_as_a_boundary_before_its_paragraph():
    body = "# Summary\nThis data control defines the review cadence.\n## Notes:\nKeep it short."
    passage = best_passage(body, "cadence")
    assert passage == (
        "Summary: This data control defines the review cadence. Notes: Keep it short."
    )


def test_markdown_cleanup_keeps_identifiers_and_numbers():
    body = (
        "Call snake_case_name and __init__ from my_module.\n"
        "1. First step\n"
        "2. Second step\n"
        "Price is 2*3 = 6 and a ** b stays.\n"
    )
    passage = best_passage(body, "step")
    assert "snake_case_name" in passage and "__init__" in passage and "my_module" in passage
    assert "1. First step" in passage and "2. Second step" in passage
    assert "2*3" in passage and "a ** b" in passage


def test_markdown_snippet_highlights_cleaned_text():
    body = "- **Review cadence:** Monthly\n- **Owner:** `ops-team`\n"
    segments = build_snippet(Hit("d", "t", 1.0, ""), body, "cadence owner")
    assert _highlighted(segments) == ["cadence", "Owner"]
    assert "*" not in _joined(segments) and "`" not in _joined(segments)
