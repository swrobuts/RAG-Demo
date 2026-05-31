from backend.retrieval.pageindex import _parse_id_list


def test_plain_json_array():
    assert _parse_id_list("[1, 2, 3]") == [1, 2, 3]


def test_empty_array():
    assert _parse_id_list("[]") == []


def test_array_inside_prose():
    # Some local models still wrap the array in explanation text.
    assert _parse_id_list("Hier sind die IDs: [4, 7]. Begründung: …") == [4, 7]


def test_markdown_codefence_wrapping():
    assert _parse_id_list("```json\n[12, 14]\n```") == [12, 14]
    assert _parse_id_list("```\n[1]\n```") == [1]


def test_invalid_response_returns_empty():
    assert _parse_id_list("Keine Sektion ist relevant.") == []
    assert _parse_id_list("") == []
    assert _parse_id_list("[abc]") == []


def test_floats_become_ints():
    assert _parse_id_list("[1.0, 2.0]") == [1, 2]


def test_trailing_garbage_after_array():
    assert _parse_id_list("[3, 5] und dazu noch [99]") == [3, 5]
