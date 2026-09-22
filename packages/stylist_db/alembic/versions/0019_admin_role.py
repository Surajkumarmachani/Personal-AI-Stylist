"""An admin flag, so /ops can stop being readable by everyone (Phase 12).

THE GAP THIS CLOSES IS A REAL ONE, AND THE CODE ALREADY SAID SO
---------------------------------------------------------------
`routers/ops.py` opens by explaining that every aggregate it serves goes
through a SECURITY DEFINER function — deliberately, because these numbers are
CROSS-TENANT and cannot be read under RLS. It then says:

    "In production this router still belongs behind an admin authorisation
     boundary rather than a user token. It is mounted here because Phase 5 has
     no admin [role]."

That boundary never arrived. Until this migration, any registered user could
read the whole deployment's ingest funnel, latency percentiles, DLQ depth,
correction rates and model spend — other people's operational data, from an
ordinary account.

WHY A COLUMN AND NOT A ROLES TABLE
----------------------------------
There is exactly one privilege here: see the ops endpoints. A `roles` and
`user_roles` pair would be two tables, four indexes and a join on every
request to express a single boolean, and the first thing anyone would do is
add `is_admin` as a convenience view over it. When a second privilege appears,
THAT is the point to generalise — the migration is cheap and the guess about
what the second one will be is not.

DEFAULT FALSE, AND NO WAY TO SET IT FROM THE API
------------------------------------------------
No endpoint grants admin. It is set with SQL by someone with database access,
which is the same bar as reading the data it unlocks. An API that can escalate
its own callers is the thing this migration exists to prevent.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_admin_role"
down_revision = "0018_custom_occasion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Grants access to /ops, which serves CROSS-TENANT aggregates. "
            "Set by SQL only — no endpoint grants it.",
        ),
    )
    # Partial index: admins are a handful of rows in a table that grows with
    # signups, and every request that checks this wants only the true ones.
    op.create_index(
        "ix_users_admin",
        "users",
        ["id"],
        unique=False,
        postgresql_where=sa.text("is_admin"),
    )


def downgrade() -> None:
    op.drop_index("ix_users_admin", table_name="users")
    op.drop_column("users", "is_admin")
