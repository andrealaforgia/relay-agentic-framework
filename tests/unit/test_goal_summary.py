from relay.cli.watch import goal_summary


def test_title_wins() -> None:
    assert goal_summary("Free rooms are listed", "Given x, when y, then z") == "Free rooms are listed"


def test_then_clause_extracted() -> None:
    ac = ("Given two rooms and one is booked now, when I run `rooms free`, "
          "then only the unbooked room is listed.")
    assert goal_summary("", ac) == "Only the unbooked room is listed"


def test_when_clause_fallback() -> None:
    ac = "Given a full grid, when a granule lands it disperses sideways"
    assert goal_summary("", ac) == "A granule lands it disperses sideways"


def test_plain_text_passes_through() -> None:
    assert goal_summary("", "The increment is demonstrable end to end") \
        == "The increment is demonstrable end to end"
