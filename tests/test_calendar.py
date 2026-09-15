"""Phase 8 — calendar event titles to occasions, and the confidence gate.

The plan's rule is the thing under test: below the threshold, fall back to the
default AND SAY SO, because "a confidently wrong occasion is worse than
asking". So most of this file is about what happens when the classifier is
UNSURE, which is the common case for a real calendar full of "Sync" and "1:1".
"""

from __future__ import annotations

import pytest

from stylist_domain.calendar import classify, load_rules


def test_the_rules_only_name_occasions_the_taxonomy_defines() -> None:
    """A typo here would classify an event into a value `resolve_context` then
    rejects — surfacing as a 500 on a calendar sync rather than as the typo it
    is. Validated at load, so this test is really asserting that the
    validation runs."""
    rules = load_rules()
    assert rules["rules"] and rules["default_occasion"]
    assert 0.0 < float(rules["confidence_floor"]) <= 1.0


# ------------------------------------------------------ confident matches


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Interview with Anita — backend role", "interview"),
        ("Gym session", "workout"),
        ("Rahul & Priya Sangeet", "sangeet"),
        ("Mehendi at home", "mehendi"),
        ("Flight to Delhi", "travel_day"),
        ("Diwali lunch", "festival_day"),
        ("Funeral service", "funeral"),
        ("Annual Gala — black tie", "black_tie_event"),
        ("Temple visit with Amma", "temple_visit"),
        ("Client call with Acme", "client_meeting"),
    ],
)
def test_clear_titles_classify_confidently(title: str, expected: str) -> None:
    result = classify([title])
    assert result.occasion == expected
    assert result.confident is True
    assert result.is_fallback is False


def test_a_confident_result_carries_the_dress_code_and_formality() -> None:
    """The caller feeds these straight into `resolve_context`, so they come
    from the taxonomy rather than being restated in the rules file."""
    result = classify(["Wedding ceremony"])
    assert result.occasion == "wedding_ceremony"
    assert result.dress_code == "formal_ethnic"
    assert result.formality_target == 5


# -------------------------------------------------------- the gate


def test_an_ordinary_work_calendar_falls_back_and_says_so() -> None:
    """THE RULE THIS FEATURE TURNS ON. Most calendar entries say nothing about
    clothes, and guessing office_formal from "1:1 w/ Anita" is worse than
    admitting it could not tell."""
    result = classify(["Coffee with Sam", "Lunch"])
    assert result.is_fallback is True
    assert result.confident is False
    assert result.occasion == load_rules()["default_occasion"]
    # The explanation names what it saw, so the UI can show it verbatim.
    assert "Not sure" in result.explanation


def test_an_empty_calendar_falls_back_without_pretending_to_have_matched() -> None:
    result = classify([])
    assert result.is_fallback is True
    assert result.confidence == 0.0
    assert result.matches == ()
    assert "Nothing in your calendar" in result.explanation


def test_an_unrecognisable_title_does_not_invent_an_occasion() -> None:
    result = classify(["Zzzz", "Blocked", "OOO"])
    assert result.is_fallback is True
    assert result.matches == ()


def test_the_explanation_never_contains_the_event_title() -> None:
    """Event titles are user data. The explanation is rendered in the UI and
    may end up in a screenshot or a support ticket, so it carries only the
    phrase WE matched, never the surrounding text the user wrote."""
    secret = "Divorce mediation with Dr Mehta"
    result = classify([secret, "Gym"])
    assert secret not in result.explanation
    assert "Mehta" not in result.explanation
    for match in result.matches:
        assert secret not in match.phrase


# ------------------------------------------------ several events in a day


def test_the_dressiest_confident_match_wins() -> None:
    """Nobody changes between a standup and a wedding. You dress for the
    dressiest thing on the calendar, even if it is one event among six."""
    result = classify(["Standup", "Sprint retro", "Wedding ceremony", "1:1"])
    assert result.occasion == "wedding_ceremony"
    assert result.confident is True


def test_a_confident_match_beats_many_unconfident_ones() -> None:
    """Three coffees do not add up to an occasion; one interview is one."""
    result = classify(["Coffee", "Coffee catch up", "Lunch", "Interview with Acme"])
    assert result.occasion == "interview"
    assert result.confident is True


def test_ties_break_toward_the_dressier_occasion() -> None:
    """Between two equally-sure matches, overdressed is the recoverable error."""
    result = classify(["Mehendi and sangeet"])
    assert result.confident is True
    # Both are 0.92 in the rules; sangeet is formality 5, mehendi 4.
    assert result.occasion == "sangeet"


# ------------------------------------------------- matching discipline


@pytest.mark.parametrize(
    "title",
    ["Winterview prep", "Reinterviewing candidates", "Brunch running late", "Runway show"],
)
def test_substrings_do_not_fire(title: str) -> None:
    """Substring matching turns "winterview" into an interview and "brunch"
    into a run. A false occasion is exactly the confident-and-wrong failure the
    gate exists to prevent, so the gate must not be undermined by the match."""
    result = classify([title])
    assert result.occasion != "interview" or "Interview" in title
    assert result.occasion != "workout"


def test_longer_phrases_win_over_shorter_ones() -> None:
    """ "black tie" must beat a bare "tie", and the answer must not depend on
    the order rules happen to appear in the YAML — which is not something a
    reviewer of that file would think to check."""
    result = classify(["Annual dinner — black tie"])
    assert result.occasion == "black_tie_event"


def test_matching_is_case_insensitive() -> None:
    assert classify(["INTERVIEW WITH ACME"]).occasion == "interview"
    assert classify(["interview with acme"]).occasion == "interview"


def test_classification_is_deterministic() -> None:
    """The same calendar must give the same answer on every sync, or the
    morning suggestion changes for no reason the user can see."""
    titles = ["Standup", "Client call", "Gym", "Dinner with Priya"]
    first = classify(titles)
    for _ in range(5):
        assert classify(titles) == first
