"""Phase 8 — the style vector, and the property the exit criterion turns on.

`rebuild_style_vectors.py` must reproduce the live vector from the event log,
asserted by equality. That is only achievable if the update is a pure function
of (previous vector, event) applied in a defined order — so the test that
matters here is `test_replaying_the_log_reproduces_the_live_vector`, and the
rest exist to stop it passing for a trivial reason.
"""

from __future__ import annotations

import math

import pytest

from stylist_domain.style import (
    DEFAULT_ALPHA,
    StyleVector,
    apply_event,
    normalise,
    outfit_embedding,
)

# Three orthogonal directions, so "moved toward" and "moved away" are
# unambiguous rather than a matter of floating-point luck.
E1 = (1.0, 0.0, 0.0)
E2 = (0.0, 1.0, 0.0)
E3 = (0.0, 0.0, 1.0)


def is_unit(vec: tuple[float, ...]) -> bool:
    return math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0, abs_tol=1e-9)


# ------------------------------------------------- THE exit criterion


def test_replaying_the_log_reproduces_the_live_vector() -> None:
    """PHASE 8 EXIT CRITERION: the rebuild reproduces live state exactly.

    "Live" here is the vector built by folding events in as they arrive;
    "rebuilt" is the same events replayed from scratch. They must be EQUAL,
    not merely close — approximate equality would let a real divergence hide
    under the tolerance, and the whole reason the plan demands this script be
    written now is that unrebuildable state is discovered far too late.
    """
    events = [
        ("like", E1, "e1"),
        ("worn", E2, "e2"),
        ("dislike", E3, "e3"),
        ("saved", E1, "e4"),
        ("dismissed", E2, "e5"),
        ("worn", E1, "e6"),
    ]

    live: StyleVector | None = None
    for kind, vec, eid in events:
        live = apply_event(live, kind=kind, outfit_vec=vec, event_id=eid)

    rebuilt: StyleVector | None = None
    for kind, vec, eid in events:
        rebuilt = apply_event(rebuilt, kind=kind, outfit_vec=vec, event_id=eid)

    assert live is not None and rebuilt is not None
    assert rebuilt.vector == live.vector, "bit-for-bit, not approximately"
    assert rebuilt.events_applied == live.events_applied == len(events)
    assert rebuilt.last_event_id == live.last_event_id == "e6"


def test_order_changes_the_result_which_is_why_replay_order_is_pinned() -> None:
    """EWMA is not commutative, and that is the point of the
    `(user_id, created_at, id)` index.

    If two events sharing a timestamp could replay in either order, the rebuild
    would differ from live state by an amount nobody could explain — so this
    test documents WHY the index carries `id` as a tie-break rather than
    treating order as an implementation detail.
    """
    a = apply_event(None, kind="like", outfit_vec=E1, event_id="1")
    a = apply_event(a, kind="dislike", outfit_vec=E2, event_id="2")

    b = apply_event(None, kind="dislike", outfit_vec=E2, event_id="2")
    b = apply_event(b, kind="like", outfit_vec=E1, event_id="1")

    assert a.vector != b.vector


# ------------------------------------------------------- the mechanics


def test_a_positive_event_moves_toward_the_outfit() -> None:
    v = apply_event(None, kind="like", outfit_vec=E1, event_id="1")
    assert v.cosine(list(E1)) > 0.99, "first like points straight at it"
    assert is_unit(v.vector)


def test_a_dislike_moves_away_and_is_damped() -> None:
    """Ignoring dislikes throws away the more precise half of the signal —
    people reject more accurately than they endorse. But a single rejection
    must not invert a taste built from twenty likes, so it is halved."""
    liked = apply_event(None, kind="like", outfit_vec=E1, event_id="1")
    after = apply_event(liked, kind="dislike", outfit_vec=E2, event_id="2")

    assert after.cosine(list(E2)) < 0.0, "moved away from the disliked direction"
    assert after.cosine(list(E1)) > 0.9, "but still mostly where it was"


def test_twenty_likes_outweigh_one_dislike() -> None:
    """The damping, stated as the behaviour it exists to produce."""
    v: StyleVector | None = None
    for i in range(20):
        v = apply_event(v, kind="like", outfit_vec=E1, event_id=str(i))
    before = v.cosine(list(E1))  # type: ignore[union-attr]
    after = apply_event(v, kind="dislike", outfit_vec=E2, event_id="x")

    assert after.cosine(list(E1)) > 0.9
    assert after.cosine(list(E1)) < before, "it still moved — damped is not ignored"


def test_dismissed_is_counted_but_does_not_move_the_vector() -> None:
    """Dismissing a card is "not now" or a mis-tap. Reading it as taste would
    let scrolling past something teach the system to avoid it.

    It still advances `events_applied`: the rebuild must account for every row
    it read, or the counter the two sides compare stops meaning anything.
    """
    liked = apply_event(None, kind="like", outfit_vec=E1, event_id="1")
    after = apply_event(liked, kind="dismissed", outfit_vec=E2, event_id="2")

    assert after.vector == liked.vector
    assert after.events_applied == liked.events_applied + 1
    assert after.last_event_id == "2"


def test_an_unknown_kind_is_inert_rather_than_an_error() -> None:
    """A kind added to the enum before this module knows about it must not
    take down the feedback handler — it should simply carry no taste signal."""
    liked = apply_event(None, kind="like", outfit_vec=E1, event_id="1")
    after = apply_event(liked, kind="side_eyed", outfit_vec=E2, event_id="2")
    assert after.vector == liked.vector


def test_the_vector_stays_unit_length_through_a_long_mixed_history() -> None:
    """Without renormalising, repeated negatives shrink the magnitude toward
    zero and the cosine against every garment converges on the same value — a
    vector that silently stops discriminating while still looking like one."""
    v: StyleVector | None = None
    for i in range(200):
        kind = ("like", "dislike", "worn", "saved")[i % 4]
        vec = (E1, E2, E3)[i % 3]
        v = apply_event(v, kind=kind, outfit_vec=vec, event_id=str(i))
    assert v is not None and is_unit(v.vector)


# --------------------------------------------------------- edge cases


def test_a_zero_vector_normalises_to_zero_not_nan() -> None:
    """A zero vector is a REAL state — a user with no feedback, or a perfectly
    balanced history. Dividing by its norm gives NaN, and NaN compares False
    against every threshold, so the failure would present as "this user gets no
    suggestions" rather than as arithmetic."""
    assert normalise((0.0, 0.0, 0.0)) == (0.0, 0.0, 0.0)
    assert not any(math.isnan(v) for v in normalise((0.0, 0.0)))


def test_an_outfit_embedding_is_the_normalised_mean_and_order_free() -> None:
    """Outfits are SETS. A concatenation would make [shirt, trousers] and
    [trousers, shirt] different outfits."""
    a = outfit_embedding([list(E1), list(E2)])
    b = outfit_embedding([list(E2), list(E1)])
    assert a == b
    assert a is not None and is_unit(a)


def test_an_outfit_with_no_embeddings_is_none_not_zero() -> None:
    """ "No embedding" and "sits at the origin" must not be the same value to
    the caller — one means skip this event, the other is a real position."""
    assert outfit_embedding([]) is None
    assert outfit_embedding([[], []]) is None


def test_mismatched_dimensions_raise_rather_than_silently_truncating() -> None:
    with pytest.raises(ValueError, match="dimension"):
        outfit_embedding([[1.0, 0.0], [0.0, 1.0, 0.0]])
    with pytest.raises(ValueError, match="dimension"):
        apply_event(
            StyleVector((1.0, 0.0), 1, "x", DEFAULT_ALPHA),
            kind="like",
            outfit_vec=E1,
            event_id="y",
        )


def test_cosine_does_not_assume_the_other_side_is_normalised() -> None:
    """Garment embeddings come from the database and may not be unit-length."""
    v = apply_event(None, kind="like", outfit_vec=E1, event_id="1")
    assert math.isclose(v.cosine([5.0, 0.0, 0.0]), v.cosine([1.0, 0.0, 0.0]), abs_tol=1e-9)
    assert v.cosine([0.0, 0.0, 0.0]) == 0.0, "a zero garment vector is not NaN"
