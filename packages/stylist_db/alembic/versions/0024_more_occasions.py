"""Ten more occasions: the working week, and the ceremonies around a wedding.

WHY THE TAXONOMY FREEZE DOES NOT BLOCK THIS
--------------------------------------------
`taxonomy.yaml` is frozen at v1.0.0 because its vocabularies GENERATE POSTGRES
ENUMS, and changing `slot` or `colour` would rewrite columns across the whole
schema. `occasion` is different in one decisive way: the type exists and is
kept in step with the YAML, but NO COLUMN USES IT. `outfits.occasion` and
`outfit_feedback.occasion` are varchar.

So this appends labels to a type nothing is stored as. There is no rewrite, no
backfill, and no risk to existing rows.

APPENDED, NOT INSERTED
----------------------
`test_db_enums_match_taxonomy_yaml` compares the database's labels with the
YAML's IN ORDER. `ALTER TYPE ... ADD VALUE` appends, so the ten new ids sit at
the end of the YAML list too. Putting one in the middle would mean recreating
the type — for a vocabulary nothing is stored as, that would be work done only
to satisfy a sort order.

WHAT ELSE EACH ONE NEEDED
-------------------------
An occasion is not usable until three things exist: this label, a phrase in
`stylist_domain.intent.LEXICON` so it can be typed, and a tile in
`web/app/OCCASIONS.ts` so it can be tapped. Twelve of the original eighteen
had tiles, which is how `office_formal`, `client_meeting` and `wfh` — most of
the working week — stayed unreachable from the screen while the resolver
understood them perfectly.
"""

from __future__ import annotations

from alembic import op

revision = "0024_more_occasions"
down_revision = "0023_owned_from_catalogue"
branch_labels = None
depends_on = None

NEW = (
    "conference",
    "networking_event",
    "office_party",
    "team_offsite",
    "haldi",
    "engagement",
    "griha_pravesh",
    "baby_shower",
    "brunch",
    "graduation",
)


def upgrade() -> None:
    for label in NEW:
        # IF NOT EXISTS so a re-run is harmless; Postgres 12+ allows this
        # inside a transaction as long as the value is not used in the same
        # one, which it is not.
        op.execute(f"ALTER TYPE occasion ADD VALUE IF NOT EXISTS '{label}'")


def downgrade() -> None:
    # POSTGRES CANNOT DROP AN ENUM LABEL. The honest options are to recreate
    # the type or to do nothing, and recreating a type nothing is stored as,
    # to remove labels nothing references, would be ceremony with a rewrite
    # risk attached. Stated rather than silently passing.
    pass
