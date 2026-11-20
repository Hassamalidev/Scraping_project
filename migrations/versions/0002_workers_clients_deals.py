"""worker sign-ups, client job requests and deals

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("businesses", sa.Column("signed_up_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("businesses", sa.Column("contact_name", sa.Text(), nullable=True))
    op.add_column("phones", sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "job_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("phone_e164", sa.String(length=20), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("trade", sa.String(length=40), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("zip", sa.String(length=10), nullable=True),
        sa.Column("timing", sa.String(length=40), nullable=True),
        sa.Column("budget", sa.Text(), nullable=True),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("consent_text", sa.Text(), nullable=False),
        sa.Column("consent_version", sa.String(length=20), nullable=False),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consent_ip", sa.String(length=64), nullable=True),
        sa.Column("consent_user_agent", sa.Text(), nullable=True),
        sa.Column("consent_page_url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("last_called_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.Integer(), nullable=True),
        sa.Column("client_price", sa.Numeric(10, 2), nullable=True),
        sa.Column("worker_price", sa.Numeric(10, 2), nullable=True),
        sa.Column("payment_method", sa.String(length=20), nullable=True),
        sa.Column("payment_status", sa.String(length=20), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("consent_at IS NOT NULL", name="ck_job_request_consent"),
        sa.ForeignKeyConstraint(["worker_id"], ["businesses.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_requests_phone_e164", "job_requests", ["phone_e164"])
    op.create_index("ix_job_requests_trade", "job_requests", ["trade"])
    op.create_index("ix_job_requests_status", "job_requests", ["status"])


def downgrade() -> None:
    op.drop_index("ix_job_requests_status", table_name="job_requests")
    op.drop_index("ix_job_requests_trade", table_name="job_requests")
    op.drop_index("ix_job_requests_phone_e164", table_name="job_requests")
    op.drop_table("job_requests")
    op.drop_column("phones", "consent_at")
    op.drop_column("businesses", "contact_name")
    op.drop_column("businesses", "signed_up_at")
