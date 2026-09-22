"""The name of the city behind `home_lat_2dp` / `home_lon_2dp` (Phase 6.1, wired Phase 12).

WHY A LABEL WHEN THE COORDINATES ARE ALREADY THERE
--------------------------------------------------
The two coordinate columns have existed since Phase 6 and nothing ever wrote
them, because there was no way to ask the user where they live. Wiring that up
needs one more thing the coordinates cannot provide: a name to show back.

"25.59, 85.14" is not reviewable. The weather that drives `weather_fit` is only
trustworthy if the user can see WHICH place it came from and correct it, and
the failure this guards against is specific and measured: the live geocoder
resolves "Bangalore" to `Bangalore Town, Sindh, PAKISTAN` — the Indian city is
indexed as Bengaluru, so no ranking rule finds it. A user who typed
"Bangalore" would get Sindh's weather forever, with nothing on screen to
reveal it. Storing the resolved label is what makes that visible.

NOT DERIVED, SO IT IS STORED
----------------------------
It could be re-geocoded from the coordinates on every read. That would be a
third-party call on a request path to recover a string we already had, and
reverse geocoding does not reliably return the same label the user confirmed.

STILL 2dp, DELIBERATELY
-----------------------
This migration adds no precision. The columns stay numeric(5,2) (~1.1km), which
is the privacy boundary `stylist_clients.weather` documents and the cache key it
depends on. A city label is coarser than the coordinates, not finer.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_home_place"
down_revision = "0016_bandit_arm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_profile",
        sa.Column(
            "home_place",
            sa.String(160),
            nullable=True,
            comment="Resolved city label, e.g. 'Patna, Bihar, India'. Shown to the "
            "user so a wrong geocode is visible and correctable. NULL means "
            "no location has been set and suggestions use the placeholder.",
        ),
    )


def downgrade() -> None:
    op.drop_column("user_profile", "home_place")
