"""The Hiring Manager role (22 Oct 2026).

User ask: a new role, Hiring Manager — a Karnex employee who sees ONLY the
opportunities assigned to them, approves the JD & skills before TA sources,
reviews each candidate before the profile goes to the customer, takes
interviews, may ask for a headcount change, sets priority / hold on their
positions, and signs off the final selection before the Sales Head approves
the terms. GM / RMG assign them at RMG approval (several per position allowed).

1. `requirement_status` gains **Pending_HM_Review** (between RMG approval and
   Open_For_Sourcing). Native enum → its own autocommit block.
2. `requirement_hiring_managers` — one row per (requirement, HM login). Unlike
   the TA table it IS a visibility rule (services/hiring_managers.py).
3. `candidate_profiles.hm_review_*` and `hm_final_*` — the two HM gates.
   NULL = no HM on the position, so every existing candidacy is unaffected.
4. The **Hiring Manager** custom role is SEEDED (like the Interviewer, 0128):
   view of Dashboard · Opportunities · Requirements · Candidate Profiles, My
   Interviews to record feedback, and its approvals. Skipped when a role of
   that name exists (any case) — Admin's own role is never overwritten.

`_APPROVAL_DEFAULTS` is the frozen copy of the three new approvals' code
defaults (pinned against `action_permissions.ACTIONS` by
`test_approval_permissions`); the seeded role's action list is built from it.

Revision ID: 0145
Revises: 0144
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0145"
down_revision = "0144"
branch_labels = None
depends_on = None

ROLE_NAME = "Hiring Manager"
ROLE_DESCRIPTION = ("Karnex hiring manager: sees only the positions assigned to them, approves the JD & "
                    "skills before sourcing, reviews candidates before the customer sees them, takes "
                    "interviews and signs off the final selection before the Sales Head.")
TAB_ACCESS = {"dashboard": "view", "opportunities": "view", "requirements": "view",
              "profiles": "view", "my-interviews": "edit"}

#: Frozen copy of the approvals this revision added (key → default roles).
_APPROVAL_DEFAULTS = {
    "requirement.hm_approve": ("Hiring Manager",),
    "profile.hm_review": ("Hiring Manager",),
    "profile.hm_signoff": ("Hiring Manager",),
}
#: The seeded role's buttons: its three approvals + asking for a headcount change.
ROLE_ACTIONS = list(_APPROVAL_DEFAULTS) + ["requirement.positions.request"]

_PROFILE_COLUMNS = (
    ("hm_review_status", sa.String(16)), ("hm_review_note", sa.Text()),
    ("hm_review_by", sa.Integer()), ("hm_review_at", sa.DateTime(timezone=True)),
    ("hm_final_status", sa.String(16)), ("hm_final_note", sa.Text()),
    ("hm_final_by", sa.Integer()), ("hm_final_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Native enum: the new value must be committed before anything uses it.
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE requirement_status ADD VALUE IF NOT EXISTS 'Pending_HM_Review'")

    op.create_table(
        "requirement_hiring_managers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("requirement_id", sa.Integer,
                  sa.ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("registration_data.id"), nullable=False, index=True),
        sa.Column("user_name", sa.String(255), nullable=True),
        sa.Column("assigned_by", sa.Integer, sa.ForeignKey("registration_data.id"), nullable=True),
        sa.Column("assigned_by_name", sa.String(255), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.UniqueConstraint("requirement_id", "user_id", name="uq_req_hiring_manager"),
    )

    for name, type_ in _PROFILE_COLUMNS:
        op.add_column("candidate_profiles", sa.Column(name, type_, nullable=True))
    op.create_index("ix_candidate_profiles_hm_review_status", "candidate_profiles", ["hm_review_status"])
    op.create_index("ix_candidate_profiles_hm_final_status", "candidate_profiles", ["hm_final_status"])

    exists = bind.execute(
        sa.text("SELECT id FROM custom_roles WHERE lower(name) = lower(:n)"), {"n": ROLE_NAME}
    ).scalar()
    if exists is None:
        cols = {c["name"] for c in sa.inspect(bind).get_columns("custom_roles")}
        values = {"n": ROLE_NAME, "d": ROLE_DESCRIPTION, "active": True,
                  "tabs": json.dumps(TAB_ACCESS), "fields": json.dumps({}),
                  "actions": json.dumps(ROLE_ACTIONS)}
        extra_cols, extra_vals = "", ""
        if "department" in cols:
            extra_cols, extra_vals = ", department", ", :dept"
            values["dept"] = "engineering"
        bind.execute(
            sa.text("INSERT INTO custom_roles (name, description, is_active, tab_access, field_access, "
                    f"action_access, created_at, updated_at{extra_cols}) "
                    "VALUES (:n, :d, :active, :tabs, :fields, :actions, CURRENT_TIMESTAMP, "
                    f"CURRENT_TIMESTAMP{extra_vals})"),
            values,
        )


def downgrade() -> None:
    # The seeded role stays (members may hold it); the enum value cannot be
    # dropped from a native Postgres enum.
    op.drop_index("ix_candidate_profiles_hm_final_status", table_name="candidate_profiles")
    op.drop_index("ix_candidate_profiles_hm_review_status", table_name="candidate_profiles")
    for name, _ in reversed(_PROFILE_COLUMNS):
        op.drop_column("candidate_profiles", name)
    op.drop_table("requirement_hiring_managers")
