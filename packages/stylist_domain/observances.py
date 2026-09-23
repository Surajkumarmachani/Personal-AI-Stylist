"""Which day it is, for people whose own calendar is empty.

Occasion resolution ran: the user's Google calendar, else `casual_outing`. So
on Diwali, a user who had not connected a calendar was offered an everyday
casual look -- from information the system already had, since the date is not
personal data and needs no integration.

This is the middle step. It is PURE: the table is `config/observances.yaml`
and the only input is a date, so it is testable without a clock, a database or
a network.

WHAT IT REFUSES TO DO
---------------------
It does not guess. Lunar and lunisolar festivals move every year and cannot be
derived from a rule this system could implement correctly, so they are listed
explicitly and the table states how far it reaches. Past `coverage_until`,
`observance_for` returns None AND `is_stale` says why -- a calendar that
quietly stops working, leaving every day looking ordinary, is worse than one
that admits it is out of data.
"""

from __future__ import annotations

import datetime as dt
import functools
import pathlib
import re
from dataclasses import dataclass
from typing import Any

import yaml

CONFIG = pathlib.Path(__file__).resolve().parents[2] / "config" / "observances.yaml"


@dataclass(frozen=True, slots=True)
class Observance:
    """One day that carries a dress expectation."""

    name: str
    occasion: str
    note: str | None = None


@functools.lru_cache(maxsize=1)
def _table() -> dict[str, Any]:
    with CONFIG.open() as fh:
        return dict(yaml.safe_load(fh))


def coverage_until() -> dt.date:
    """Last date the moving-festival entries are complete to."""
    value = _table()["coverage_until"]
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))


def is_stale(on: dt.date) -> bool:
    """True when `on` is past what anyone has actually entered.

    Recurring entries still resolve past this date -- Independence Day does not
    stop being the 15th of August -- but the moving ones are unknown, so a
    "no observance" answer for such a date means "not in the table", not "an
    ordinary day". The caller is entitled to tell those apart.
    """
    return on > coverage_until()


def observance_for(on: dt.date) -> Observance | None:
    """The observance falling on this date, or None.

    Dated entries win over recurring ones: if someone has written an explicit
    row for a date, it is more specific than a rule that happens to also match.
    """
    table = _table()

    for row in table.get("dated") or []:
        raw = row["date"]
        when = raw if isinstance(raw, dt.date) else dt.date.fromisoformat(str(raw))
        if when == on:
            return Observance(row["name"], row["occasion"], row.get("note"))

    stamp = f"{on.month:02d}-{on.day:02d}"
    for row in table.get("recurring") or []:
        if str(row["month_day"]) == stamp:
            return Observance(row["name"], row["occasion"], row.get("note"))
    return None


def occasion_for_holiday_name(name: str) -> tuple[str, bool]:
    """Map a public-holiday name to a taxonomy occasion.

    Returns (occasion, recognised). `recognised` is False when nothing matched
    and the default was used -- the caller can then phrase it as a guess
    instead of a statement. A bank holiday is not a festival, and telling
    someone their outfit is "dressed for Spring Bank Holiday" with the
    confidence of Diwali would be wrong in a way they would notice.

    WHOLE WORDS, case-insensitive, in file order.
    -------------------------------------------
    Naive substring matching was the first version and it was wrong in a way
    that would have shipped: "holi" is inside "Bank **Holi**day", so every UK
    bank holiday resolved to Holi and would have dressed people in festive
    ethnic wear for a long weekend. Exact matching is no good either --
    Google's summaries vary by locale and carry parentheticals ("Holi
    (Festival of Colours)", "Diwali/Deepavali") -- so the rule is: the
    configured phrase must appear as whole words.
    """
    table = _table()
    needle = name.casefold()
    for row in table.get("holiday_occasions") or []:
        phrase = str(row["match"]).casefold()
        if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", needle):
            return str(row["occasion"]), True
    return str(table.get("holiday_default_occasion", "festival_day")), False
