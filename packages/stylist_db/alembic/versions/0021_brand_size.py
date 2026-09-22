"""Brand and size on a garment (Phase 12).

WHY THESE ARE FREE TEXT AND NOT TAXONOMY ENUMS
-----------------------------------------------
Every other descriptive field on a garment — slot, colour, pattern, material,
fit, dress_code — is a closed vocabulary in `taxonomy.yaml` that generates a
Postgres enum, because the VLM has to be held to a fixed set and a value
outside it is a bug. Brand and size are the opposite shape:

  brand   is open by nature. There is no complete list, a new label appears
          every week, and an enum would mean a migration per brand. It is also
          not a property the taxonomy can validate — "Fabindia" is not more or
          less correct than "Raymond".

  size    has no single system. A shirt is M, trousers are 32, shoes are UK 9
          or EU 42, and Indian kurta sizing runs 38-46 on the chest. Modelling
          that properly means a size_system enum plus per-slot rules, and the
          honest question is what would READ it: nothing in the scorer does.
          A label the user recognises beats a schema nobody queries.

SO THEY ARE NOT SCORED, AND THAT IS STATED IN THE UI
-----------------------------------------------------
Neither field feeds `score_outfit`, the candidate filters or the reranker. They
are record-keeping: what you own, in your words, for when you are shopping or
deciding what to let go of. Adding them to the ranking later is a config
change; pretending they already matter would be the kind of claim this project
keeps having to take back.

USER-ENTERED ONLY — the VLM is NOT asked for them
--------------------------------------------------
A vision model will read a logo confidently and wrongly, and a garment
labelled "Nike" that is not Nike is worse than a garment with no brand: it is
a fact the user did not enter and cannot easily disbelieve. Same for size,
which is usually on a care label the photograph does not show.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_brand_size"
down_revision = "0020_avatar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "garments",
        sa.Column(
            "brand",
            sa.String(80),
            nullable=True,
            comment="Free text, user-entered. NOT a taxonomy enum and NOT inferred "
            "by the VLM — a confidently wrong logo reading is worse than blank.",
        ),
    )
    op.add_column(
        "garments",
        sa.Column(
            "size_label",
            sa.String(40),
            nullable=True,
            comment="Free text as the user knows it: 'M', '32', 'UK 9', '42 EU'. "
            "No single size system exists across garment types or regions.",
        ),
    )


def downgrade() -> None:
    op.drop_column("garments", "size_label")
    op.drop_column("garments", "brand")
