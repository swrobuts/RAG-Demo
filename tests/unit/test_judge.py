from backend.evaluation.judge import (
    JudgeScores,
    _clamp,
    _extract_json,
    _format_sources,
    _parse_scores,
)


def test_extract_json_plain():
    obj = _extract_json('{"scores": {"ue1": {"korrektheit": 2}}}')
    assert obj is not None
    assert obj["scores"]["ue1"]["korrektheit"] == 2


def test_extract_json_with_codefence():
    obj = _extract_json('```json\n{"a": 1}\n```')
    assert obj == {"a": 1}


def test_extract_json_with_prose_around():
    text = 'Sicher, hier mein Urteil:\n{"a": 1, "b": 2}\n— so passt das.'
    assert _extract_json(text) == {"a": 1, "b": 2}


def test_extract_json_invalid_returns_none():
    assert _extract_json("kein JSON hier") is None
    assert _extract_json("") is None


def test_clamp_handles_out_of_range_and_garbage():
    assert _clamp(0) == 1
    assert _clamp(6) == 5
    assert _clamp("nope") == 3
    assert _clamp(None) == 3
    assert _clamp(3.4) == 3
    assert _clamp(3.6) == 4


def test_parse_scores_fills_defaults_for_missing_strategies():
    raw = {"scores": {"ue1": {"korrektheit": 1, "vollstaendigkeit": 2,
                                "quellenbezug": 2, "fokussiertheit": 3,
                                "kommentar": "passt"}}}
    scores = _parse_scores(raw, ["ue1", "ue2"])
    assert scores["ue1"].korrektheit == 1
    # Missing ue2 → all neutral (3)
    assert scores["ue2"].korrektheit == 3
    assert scores["ue2"].kommentar == ""


def test_judge_scores_average():
    s = JudgeScores(1, 2, 3, 4, "x")
    assert s.average == 2.5
    s2 = JudgeScores(5, 5, 5, 5, "x")
    assert s2.average == 5.0


def test_format_sources_dedupes_and_counts():
    out = _format_sources([
        {"section_path": "Geschichte"},
        {"section_path": "Geschichte"},
        {"section_path": "Produkte"},
    ])
    assert "Geschichte (×2)" in out
    assert "Produkte" in out


def test_format_sources_handles_empty():
    assert _format_sources([]) == "(keine)"
    assert "?" in _format_sources([{"section_path": None}])
