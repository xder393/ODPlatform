"""create P0 durable business state

Revision ID: 0001_p0_business_state
Revises:
Create Date: 2026-08-24
"""

import sqlalchemy as sa
from alembic import op


revision = "0001_p0_business_state"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "actors",
        sa.Column("actor_id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_actors_organization_id", "actors", ["organization_id"])
    op.create_table(
        "actor_line_grants",
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("actors.actor_id"), primary_key=True),
        sa.Column("line_id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
    )
    op.create_index("ix_actor_line_grants_organization_id", "actor_line_grants", ["organization_id"])
    op.create_table(
        "password_credentials",
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("actors.actor_id"), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("password_hash", sa.String(512), nullable=False),
    )
    op.create_index("ix_password_credentials_organization_id", "password_credentials", ["organization_id"])
    op.create_table(
        "defect_cases",
        sa.Column("case_id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("assignee_id", sa.Uuid(), nullable=True),
        sa.Column("last_transition_actor_id", sa.Uuid(), nullable=True),
        sa.Column("line_id", sa.Uuid(), nullable=True),
        sa.Column("product_category", sa.String(255), nullable=True),
    )
    op.create_index("ix_defect_cases_organization_id", "defect_cases", ["organization_id"])
    op.create_index("ix_defect_cases_line_id", "defect_cases", ["line_id"])
    op.create_table(
        "inspection_events",
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), sa.ForeignKey("defect_cases.case_id"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("defect_class", sa.String(255), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("model_release", sa.String(255), nullable=False),
        sa.Column("preprocessing_parameters", sa.JSON(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("input_frame_sha256", sa.String(64), nullable=False),
        sa.Column("line_id", sa.Uuid(), nullable=True),
    )
    op.create_index("ix_inspection_events_case_id", "inspection_events", ["case_id"])
    op.create_index("ix_inspection_events_organization_id", "inspection_events", ["organization_id"])
    op.create_index("ix_inspection_events_line_id", "inspection_events", ["line_id"])
    op.create_table(
        "case_transitions",
        sa.Column("transition_id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), sa.ForeignKey("defect_cases.case_id"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=False),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_case_transitions_case_id", "case_transitions", ["case_id"])
    op.create_index("ix_case_transitions_organization_id", "case_transitions", ["organization_id"])
    op.create_table(
        "audit_chain_heads",
        sa.Column("organization_id", sa.Uuid(), primary_key=True),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.Column("head_hash", sa.String(64), nullable=False),
    )
    op.create_table(
        "audit_logs",
        sa.Column("audit_id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("resource_type", sa.String(128), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("change_summary", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=True),
        sa.Column("request_ip", sa.String(64), nullable=True),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("entry_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("organization_id", "sequence"),
    )
    op.create_index("ix_audit_logs_organization_id", "audit_logs", ["organization_id"])
    op.create_table(
        "alerts",
        sa.Column("alert_id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("inspection_events.event_id"), nullable=True),
        sa.Column("line_id", sa.Uuid(), nullable=True),
        sa.Column("alert_type", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_alerts_organization_id", "alerts", ["organization_id"])
    op.create_index("ix_alerts_line_id", "alerts", ["line_id"])
    op.create_table(
        "websocket_tickets",
        sa.Column("ticket_id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("actors.actor_id"), nullable=False),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_websocket_tickets_organization_id", "websocket_tickets", ["organization_id"])


def downgrade() -> None:
    op.drop_table("websocket_tickets")
    op.drop_table("alerts")
    op.drop_table("audit_logs")
    op.drop_table("audit_chain_heads")
    op.drop_table("case_transitions")
    op.drop_table("inspection_events")
    op.drop_table("defect_cases")
    op.drop_table("password_credentials")
    op.drop_table("actor_line_grants")
    op.drop_table("actors")
