"""Which clothes to suggest to buy, and which a product is (Phase 13).

WHY THIS EXISTS
---------------
Outfits come from the user's own wardrobe, so they never needed to know who
the wardrobe belongs to. The two paths that reach PAST the wardrobe did: the
shop gap-filler offered a lehenga skirt and a sherwani side by side to the same
empty wardrobe, and the shortfall advice described a festival outfit for
nobody in particular. Both are answers to "what should I buy/wear", and an
answer for the wrong person is not an answer.

A CLOTHING PREFERENCE, NOT A GENDER RECORD
------------------------------------------
`dresses_as` asks the question the feature actually needs — whose clothes to
show — rather than recording who someone is. The values are the two lines
retailers sell plus `all`, which is a real answer (people shop both, and
nobody should be forced to pick one to use the app), not a "prefer not to
say" placeholder. NULL means an account made before the question existed and
is treated as `all` until the user answers.

It is used for two things only, both named in the UI where it is asked: which
products the gap-filler offers, and which garments the shortfall advice
names.

PRODUCT `gender` IS THE MERCHANT'S DEPARTMENT
---------------------------------------------
`women`, `men` or `unisex`, as the retailer files it. NULL is treated as
unisex — a product we cannot place is shown to everyone rather than hidden
from everyone, which matches how the catalogue treats a NULL dress code.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_dresses_as"
down_revision = "0025_shop_conversions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_profile",
        sa.Column(
            "dresses_as",
            sa.String(8),
            nullable=True,
            comment="'women', 'men' or 'all': whose clothes to suggest buying. "
            "NULL = not asked yet, treated as 'all'.",
        ),
    )
    op.create_check_constraint(
        "ck_user_profile_dresses_as",
        "user_profile",
        "dresses_as IS NULL OR dresses_as IN ('women', 'men', 'all')",
    )
    op.add_column(
        "product",
        sa.Column(
            "gender",
            sa.String(8),
            nullable=True,
            comment="The merchant's department: 'women', 'men' or 'unisex'. NULL = unisex.",
        ),
    )
    op.create_check_constraint(
        "ck_product_gender",
        "product",
        "gender IS NULL OR gender IN ('women', 'men', 'unisex')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_product_gender", "product", type_="check")
    op.drop_column("product", "gender")
    op.drop_constraint("ck_user_profile_dresses_as", "user_profile", type_="check")
    op.drop_column("user_profile", "dresses_as")
