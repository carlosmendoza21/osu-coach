"""drop the shared id-0 score

osu! sends id 0 for plays without a score id of their own (every failed play, and
passed plays it hasn't processed yet). Syncs used to save them as they were, so every
such play, of every player, landed in one row with id 0, each overwriting the last. Sync
now keys them properly (osu_coach.sync.keyed); this removes that one shared row, whose
player and play can't be trusted. The plays it stood for come back, correctly, on the
players' next syncs.

Revision ID: 656c2b0ee3e6
Revises: 3a01ad1786e9
Create Date: 2026-11-28 00:08:26.626777
"""

from collections.abc import Sequence

from alembic import op

revision: str = "656c2b0ee3e6"
down_revision: str | None = "3a01ad1786e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM scores WHERE id = 0")


def downgrade() -> None:
    # Nothing to restore: the row was a mix of different plays.
    pass
