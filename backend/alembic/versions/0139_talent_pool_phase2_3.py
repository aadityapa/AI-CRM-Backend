"""Talent Pool Phases 2 + 3 + retention / opt-out (17 Oct 2026).

* `skill_synonyms`        admin-kept "same skill" pairs (added to the built-in groups)
* `talent_skill_index`    one row per (candidate, skill, evidence) — derived, rebuilt
                          by the `talent_pool_index` job; empty until the first run
                          (the search falls back to the Phase 1 live scan meanwhile)
* `talent_saved_searches` saved searches + their daily alert state
* `candidates.pool_*`     opt-out (who / when / why / how), Keep (resets the
                          retention clock), expiry (out of the pool, record kept)

Nothing is backfilled: no candidate is opted out, kept or expired by the deploy.
The retention job decides expiry from the candidate's own activity on its first
run, warning 30 days ahead first.

Revision ID: 0139
Revises: 0138
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0139"
down_revision = "0138"
branch_labels = None
depends_on = None

_CANDIDATE_COLUMNS = (
    ("pool_opt_out_at", sa.DateTime(timezone=True)),
    ("pool_opt_out_source", sa.String(16)),
    ("pool_opt_out_reason", sa.String(500)),
    ("pool_opt_out_by", sa.Integer()),
    ("pool_kept_at", sa.DateTime(timezone=True)),
    ("pool_kept_by", sa.Integer()),
    ("pool_keep_note", sa.String(500)),
    ("pool_expired_at", sa.DateTime(timezone=True)),
    ("pool_expiry_warned_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    op.create_table(
        "skill_synonyms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("canonical", sa.String(120), nullable=False),
        sa.Column("synonym", sa.String(120), nullable=False),
        sa.Column("synonym_key", sa.String(120), nullable=False),
        sa.Column("canonical_key", sa.String(120), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("synonym_key", name="uq_skill_synonyms_key"),
    )
    op.create_index("ix_skill_synonyms_canonical_key", "skill_synonyms", ["canonical_key"])

    op.create_table(
        "talent_skill_index",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("skill_key", sa.String(120), nullable=False),
        sa.Column("skill_name", sa.String(120), nullable=False),
        sa.Column("evidence", sa.String(16), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("candidate_id", "skill_key", "evidence", name="uq_talent_skill_index_row"),
    )
    op.create_index("ix_talent_skill_index_candidate_id", "talent_skill_index", ["candidate_id"])
    op.create_index("ix_talent_skill_index_skill_key", "talent_skill_index", ["skill_key"])

    op.create_table(
        "talent_saved_searches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("filters", postgresql.JSONB(), nullable=False),
        sa.Column("alerts_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("seen_keys", postgresql.JSONB(), nullable=True),
        sa.Column("new_keys", postgresql.JSONB(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_alert_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_result_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_talent_saved_searches_user_id", "talent_saved_searches", ["user_id"])

    for name, type_ in _CANDIDATE_COLUMNS:
        op.add_column("candidates", sa.Column(name, type_, nullable=True))
    op.create_index("ix_candidates_pool_opt_out_at", "candidates", ["pool_opt_out_at"])
    op.create_index("ix_candidates_pool_expired_at", "candidates", ["pool_expired_at"])


def downgrade() -> None:
    op.drop_index("ix_candidates_pool_expired_at", table_name="candidates")
    op.drop_index("ix_candidates_pool_opt_out_at", table_name="candidates")
    for name, _ in reversed(_CANDIDATE_COLUMNS):
        op.drop_column("candidates", name)
    op.drop_index("ix_talent_saved_searches_user_id", table_name="talent_saved_searches")
    op.drop_table("talent_saved_searches")
    op.drop_index("ix_talent_skill_index_skill_key", table_name="talent_skill_index")
    op.drop_index("ix_talent_skill_index_candidate_id", table_name="talent_skill_index")
    op.drop_table("talent_skill_index")
    op.drop_index("ix_skill_synonyms_canonical_key", table_name="skill_synonyms")
    op.drop_table("skill_synonyms")
