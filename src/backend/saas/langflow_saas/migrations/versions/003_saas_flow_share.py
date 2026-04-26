"""Create saas_flow_share table for per-flow permission grants.

Revision ID: 003saas
Revises: 002saas
Create Date: 2026-04-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "003saas"
down_revision = "002saas"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    if _has_table("saas_flow_share"):
        return

    op.create_table(
        "saas_flow_share",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("flow_id", sa.Uuid(), nullable=False),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey("saas_organization.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "shared_with_user_id",
            sa.Uuid(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "permission",
            sa.Enum("READ", "RUN", "EDIT", name="flowsharepermission"),
            nullable=False,
        ),
        sa.Column(
            "shared_by",
            sa.Uuid(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_index("ix_saas_flow_share_flow_id", "saas_flow_share", ["flow_id"])
    op.create_index("ix_saas_flow_share_org_id", "saas_flow_share", ["org_id"])
    op.create_unique_constraint(
        "uq_saas_flow_share",
        "saas_flow_share",
        ["flow_id", "org_id", "shared_with_user_id", "permission"],
    )


def downgrade() -> None:
    op.drop_table("saas_flow_share")
