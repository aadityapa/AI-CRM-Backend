"""Merge duplicate leave_policy_types (Casual vs Casual Leave, etc.).

seed_crm.py historically inserted short names (Casual, Sick, Earned, …) while
migration 0038 seeded the professional long forms (Casual Leave, Sick Leave, …).
Both coexisted, so Leave Type dropdowns showed near-duplicates.

This migration:
  1. Renames typos / short aliases to a single canonical name when the
     canonical row does not yet exist.
  2. When both alias and canonical exist, re-points every leave_type_id FK
     from the alias to the canonical (merging numeric balance rows on unique
     conflicts), then deletes the alias row.
  3. Normalizes "Loss Off Pay" → "Loss of Pay".

Revision ID: 0041
Revises: 0040
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

# alias (case-insensitive exact match) → canonical display name
_ALIAS_TO_CANONICAL = (
    ("casual", "Casual Leave"),
    ("casual leave", "Casual Leave"),
    ("sick", "Sick Leave"),
    ("sick leave", "Sick Leave"),
    ("earned", "Earned Leave"),
    ("earned leave", "Earned Leave"),
    ("maternity", "Maternity Leave"),
    ("maternity leave", "Maternity Leave"),
    ("paternity", "Paternity Leave"),
    ("paternity leave", "Paternity Leave"),
    ("paid", "Paid Leave"),
    ("paid leave", "Paid Leave"),
    ("comp-off", "Comp-Off"),
    ("comp off", "Comp-Off"),
    ("compoff", "Comp-Off"),
    ("loss of pay", "Loss of Pay"),
    ("loss off pay", "Loss of Pay"),
    ("lop", "Loss of Pay"),
)

# Tables where leave_type_id participates in a UNIQUE constraint with other cols.
# On conflict we merge numeric columns into the keeper and delete the alias row.
_BALANCE_MERGE = (
    # (table, unique_cols_excluding_leave_type_id, numeric_cols_to_sum, recompute_balance_as)
    (
        "employee_leave_balances",
        ("employee_id", "year"),
        ("accrued", "consumed", "carry_forward"),
        "accrued + carry_forward - consumed",
    ),
    (
        "project_employee_leave_details",
        ("project_employee_id",),
        ("initial_balance", "opening_balance", "leave_accrual", "leave_consumed", "leave_balance"),
        None,  # keep summed leave_balance as-is
    ),
)

# Policy tables: on unique conflict keep the canonical row, drop the alias row.
_POLICY_DROP_ON_CONFLICT = (
    ("customer_leave_policies", ("customer_id", "branch_id")),
    ("project_leave_policies", ("project_id",)),
)

# Simple FK re-point (no uniqueness involving leave_type_id alone).
_SIMPLE_REPOINT = (
    "leave_applications",
    "leave_accrual_events",
)


def _ids_by_lower_name(conn) -> dict[str, int]:
    rows = conn.execute(sa.text("SELECT id, name FROM leave_policy_types")).fetchall()
    return {str(name).strip().lower(): int(id_) for id_, name in rows}


def _ensure_canonical(conn, canonical: str) -> int:
    """Return id of canonical name, creating the row if missing."""
    by = _ids_by_lower_name(conn)
    key = canonical.strip().lower()
    if key in by:
        return by[key]
    conn.execute(
        sa.text("INSERT INTO leave_policy_types (name) VALUES (:name)"),
        {"name": canonical},
    )
    by = _ids_by_lower_name(conn)
    return by[key]


def _merge_balance_table(conn, table, key_cols, num_cols, balance_expr, alias_id, keep_id):
    # Find overlapping keys
    overlap_sql = f"""
        SELECT a.id AS alias_row_id, k.id AS keep_row_id
        FROM {table} a
        JOIN {table} k
          ON {" AND ".join(f"a.{c} IS NOT DISTINCT FROM k.{c}" for c in key_cols)}
         AND k.leave_type_id = :keep_id
        WHERE a.leave_type_id = :alias_id
    """
    overlaps = conn.execute(
        sa.text(overlap_sql), {"alias_id": alias_id, "keep_id": keep_id}
    ).fetchall()
    for alias_row_id, keep_row_id in overlaps:
        sets = ", ".join(f"{c} = COALESCE(k.{c}, 0) + COALESCE(a.{c}, 0)" for c in num_cols)
        conn.execute(
            sa.text(
                f"""
                UPDATE {table} AS k
                SET {sets}
                FROM {table} AS a
                WHERE k.id = :keep_row_id AND a.id = :alias_row_id
                """
            ),
            {"keep_row_id": keep_row_id, "alias_row_id": alias_row_id},
        )
        if balance_expr:
            # Recompute balance from summed parts on the keeper.
            conn.execute(
                sa.text(
                    f"""
                    UPDATE {table}
                    SET balance = accrued + carry_forward - consumed
                    WHERE id = :keep_row_id
                    """
                ),
                {"keep_row_id": keep_row_id},
            )
        conn.execute(
            sa.text(f"DELETE FROM {table} WHERE id = :alias_row_id"),
            {"alias_row_id": alias_row_id},
        )
    # Re-point remaining alias rows
    conn.execute(
        sa.text(
            f"UPDATE {table} SET leave_type_id = :keep_id WHERE leave_type_id = :alias_id"
        ),
        {"keep_id": keep_id, "alias_id": alias_id},
    )


def _drop_policy_on_conflict(conn, table, key_cols, alias_id, keep_id):
    overlap_sql = f"""
        SELECT a.id AS alias_row_id
        FROM {table} a
        JOIN {table} k
          ON {" AND ".join(f"a.{c} IS NOT DISTINCT FROM k.{c}" for c in key_cols)}
         AND k.leave_type_id = :keep_id
        WHERE a.leave_type_id = :alias_id
    """
    for (alias_row_id,) in conn.execute(
        sa.text(overlap_sql), {"alias_id": alias_id, "keep_id": keep_id}
    ):
        # Concepts cascade from customer_leave_policies; project policies have none.
        if table == "customer_leave_policies":
            conn.execute(
                sa.text("DELETE FROM leave_credit_concepts WHERE policy_id = :id"),
                {"id": alias_row_id},
            )
        conn.execute(
            sa.text(f"DELETE FROM {table} WHERE id = :id"), {"id": alias_row_id}
        )
    conn.execute(
        sa.text(
            f"UPDATE {table} SET leave_type_id = :keep_id WHERE leave_type_id = :alias_id"
        ),
        {"keep_id": keep_id, "alias_id": alias_id},
    )


def _merge_alias_into_canonical(conn, alias_id: int, keep_id: int) -> None:
    if alias_id == keep_id:
        return
    for table, key_cols, num_cols, balance_expr in _BALANCE_MERGE:
        _merge_balance_table(conn, table, key_cols, num_cols, balance_expr, alias_id, keep_id)
    for table, key_cols in _POLICY_DROP_ON_CONFLICT:
        _drop_policy_on_conflict(conn, table, key_cols, alias_id, keep_id)
    for table in _SIMPLE_REPOINT:
        conn.execute(
            sa.text(
                f"UPDATE {table} SET leave_type_id = :keep_id WHERE leave_type_id = :alias_id"
            ),
            {"keep_id": keep_id, "alias_id": alias_id},
        )
    # Null out optional FK on PE leave details pointing at deleted customer policies already handled
    conn.execute(
        sa.text("DELETE FROM leave_policy_types WHERE id = :alias_id"),
        {"alias_id": alias_id},
    )


def upgrade() -> None:
    conn = op.get_bind()

    # Group aliases by canonical and process once per canonical target.
    by_canonical: dict[str, set[str]] = {}
    for alias, canonical in _ALIAS_TO_CANONICAL:
        by_canonical.setdefault(canonical, set()).add(alias.strip().lower())

    for canonical, alias_keys in by_canonical.items():
        keep_id = _ensure_canonical(conn, canonical)
        # Fresh lookup each round (ids change as we delete)
        by_name = _ids_by_lower_name(conn)
        keep_id = by_name[canonical.strip().lower()]
        for key in sorted(alias_keys):
            by_name = _ids_by_lower_name(conn)
            alias_id = by_name.get(key)
            if alias_id is None or alias_id == keep_id:
                continue
            # If the only difference is casing / exact canonical already handled
            if key == canonical.strip().lower():
                continue
            _merge_alias_into_canonical(conn, alias_id, keep_id)

    # Final pass: any remaining rows whose lower(name) collides (true duplicates
    # with identical names can't exist due to unique; this catches stray casing).
    rows = conn.execute(
        sa.text("SELECT id, name FROM leave_policy_types ORDER BY id")
    ).fetchall()
    seen: dict[str, int] = {}
    for id_, name in rows:
        key = str(name).strip().lower()
        if key in seen:
            _merge_alias_into_canonical(conn, int(id_), seen[key])
        else:
            seen[key] = int(id_)


def downgrade() -> None:
    # Irreversible data merge — short aliases are not restored.
    pass
