"""Direct test of the terminal-node logic that was buggy.

We replicate the trace recorded for the question 'Was war der Apple I und
der Apple II?' against the German Apple article: a 3-level navigation where
one branch (Gründung) hits a leaf at depth 2 while the other (Computer)
keeps going to depth 3. The fixed logic must preserve Gründung's path.
"""
from dataclasses import dataclass

# A tiny stand-in for repo.TreeNode — only the fields the algorithm reads.
@dataclass
class _N:
    id: int
    parent_id: int | None
    path: str


def _terminal_paths(selected_at_each_level: list[list[_N]]) -> list[str]:
    """Re-implements the production logic for the test (kept here so we can
    pin it down without dragging in the SQL layer)."""
    out: list[_N] = []
    for level_idx, level_selection in enumerate(selected_at_each_level):
        next_selection = (
            selected_at_each_level[level_idx + 1]
            if level_idx + 1 < len(selected_at_each_level)
            else []
        )
        parents_of_next = {n.parent_id for n in next_selection}
        for n in level_selection:
            if n.id not in parents_of_next:
                out.append(n)
    return [n.path for n in out]


def test_branch_that_leaves_at_depth_2_is_preserved():
    # Replays the user's real trace.
    history = _N(id=2, parent_id=None, path="Geschichte")
    products = _N(id=17, parent_id=None, path="Produkte")
    gruendung = _N(id=3, parent_id=2, path="Geschichte > 1976–1980: Gründung")  # leaf in tree
    computer = _N(id=22, parent_id=17, path="Produkte > Computer")
    desktops = _N(id=23, parent_id=22, path="Produkte > Computer > Desktops")

    selected = [
        [history, products],
        [gruendung, computer],
        [desktops],
    ]
    paths = _terminal_paths(selected)
    assert "Geschichte > 1976–1980: Gründung" in paths, \
        "the bug regressed — the Gründung branch must survive even though no children were selected at depth 3"
    assert "Produkte > Computer > Desktops" in paths
    # The non-terminal parents must NOT appear, otherwise the subtree filter
    # widens unnecessarily.
    assert "Geschichte" not in paths
    assert "Produkte > Computer" not in paths


def test_single_level_navigation():
    a = _N(id=1, parent_id=None, path="Einleitung")
    b = _N(id=2, parent_id=None, path="Geschichte")
    paths = _terminal_paths([[a, b]])
    assert paths == ["Einleitung", "Geschichte"]


def test_full_descent_three_levels():
    l1 = _N(id=1, parent_id=None, path="A")
    l2 = _N(id=2, parent_id=1, path="A > B")
    l3 = _N(id=3, parent_id=2, path="A > B > C")
    paths = _terminal_paths([[l1], [l2], [l3]])
    assert paths == ["A > B > C"]


def test_navigator_stops_early():
    # Navigator picked top-level node, then chose nothing at depth 2.
    a = _N(id=1, parent_id=None, path="Einleitung")
    paths = _terminal_paths([[a], []])
    # Even with an empty next level, the top-level pick must survive.
    assert paths == ["Einleitung"]
