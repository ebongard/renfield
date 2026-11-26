"""Merge the two heads pc20261125_presence_calibration and pc20261126_kg_contact_points.

Both were written against pc20261124_kg_edge_tier_cap on parallel branches
(#1551 stage 2 B, #1240) and merged one after the other, which forked the
chain. They touch disjoint tables, so the merge carries no schema change.

Revision ID: pc20261126b_merge_heads
Revises: pc20261125_presence_calibration, pc20261126_kg_contact_points
Create Date: 2026-11-26
"""

from collections.abc import Sequence

revision: str = "pc20261126b_merge_heads"
down_revision: str | Sequence[str] | None = (
    "pc20261125_presence_calibration",
    "pc20261126_kg_contact_points",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
