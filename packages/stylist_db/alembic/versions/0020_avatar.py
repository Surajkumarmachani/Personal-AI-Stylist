"""A profile picture (Phase 12).

ITS OWN PREFIX, LIKE BODY PHOTOS AND FOR THE SAME REASON
--------------------------------------------------------
`avatar_key` points into the `avatars/` prefix, not `originals/` and not
`body/`. Phase 8 already made that argument for body photos: "a photograph of
a person is never indistinguishable from a photograph of a shirt to a prefix
operation". An avatar is a third category again — it is shown in the app
chrome on every screen, where a body photo is consented material used only for
try-on and a garment photo is inventory.

Keeping them apart is what lets erasure, consent revocation and a plain
"change my picture" each target exactly one set of objects. Deleting an avatar
must not touch the try-on consent, and revoking try-on consent must not blank
the user's face out of the top bar.

NULLABLE, AND NULL IS THE NORMAL STATE
--------------------------------------
Most accounts will never set one. The UI falls back to the first letter of the
email, which is what it already drew.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_avatar"
down_revision = "0019_admin_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_profile",
        sa.Column(
            "avatar_key",
            sa.String(512),
            nullable=True,
            comment="Object key under the avatars/ prefix. NULL means the UI "
            "draws the email initial, which is the normal state.",
        ),
    )


def downgrade() -> None:
    op.drop_column("user_profile", "avatar_key")
