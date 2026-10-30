"""Access Templates merged into Roles (27 Oct 2026, user decision "yes do it").

Before: a login's tabs came from EITHER an Access Template (assigned per user,
tagged with a role) OR its custom roles; a template and the custom role it was
tagged with could coexist, the template winning. Two screens edited the same
kind of thing. After: every role holds its own permissions —

* built-in roles gain `roles.tab_access / field_access / action_access`
  (NULL = not configured: the product's role rules, as before);
* custom roles keep theirs.

`services.access_templates.effective_access` merges the configured roles a user
HOLDS. This migration moves every ASSIGNED, ACTIVE template onto a role so each
person keeps what they could open, with a frozen planner (`plan`, PURE — the
test runs it):

1. Tagged with a built-in role X (the "Default — Sales" pattern): the template
   becomes X's own permissions — once per role, the template with the most
   assignees winning — and covers every assignee who HOLDS X. ⚠️ X's
   permissions then apply to every holder of X, including the few who had no
   template (they were following the role rules); the report names the role.
2. Tagged with a custom role R whose EVERY member is assigned the template:
   R's permissions become the template's (exact for all of them).
3. Anything else (untagged, an unknown tag, assignees outside the tagged
   role): a new custom role carrying the template's permissions, named after
   it, with those assignees as members. They may also keep other roles, whose
   permissions now ADD UP — the merge never takes access away, it can only
   widen it for someone who held both.

Inactive templates were never applied (their users followed the role rules)
and unassigned ones restricted nobody, so neither is converted. Every user's
`access_template_id` is cleared; the `access_templates` table stays as
read-only history.

Revision ID: 0149
Revises: 0148
"""
from __future__ import annotations

import json
import re

import sqlalchemy as sa
from alembic import op

revision = "0149"
down_revision = "0148"
branch_labels = None
depends_on = None

#: Frozen at this revision on purpose.
_CONFIGURABLE = ("Sales", "Sales_Head", "RMG", "TA", "HR", "Finance")
_RESERVED = {"ceo", "admin", "sales", "sales_head", "rmg", "ta", "hr", "finance", "superadmin", "super_admin"}
_DEPARTMENT = {"Sales": "sales", "Sales_Head": "sales", "TA": "recruitment", "RMG": "engineering",
               "HR": "hr", "Finance": "finance"}
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_ -]{1,39}$")


def _loads(raw):
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def role_name_for(template_name: str, taken: set[str]) -> str:
    """A valid, unused custom-role name from a template's name (PURE).
    "Default — Sales" → "Default - Sales"; a clash adds " access", then a number."""
    clean = re.sub(r"[^A-Za-z0-9_ -]+", "-", str(template_name or ""))
    clean = re.sub(r"-{2,}", "-", re.sub(r"\s+", " ", clean)).strip(" -_")
    clean = re.sub(r"\s*-\s*", " - ", clean).strip()
    if not clean or not clean[0].isalpha():
        clean = ("Access " + clean).strip()
    clean = clean[:40].strip()
    if len(clean) < 2:
        clean = "Access role"

    def ok(name: str) -> bool:
        return bool(_NAME_RE.match(name)) and name.lower() not in _RESERVED and name.lower() not in taken

    if ok(clean):
        return clean
    base = (clean[:33].strip() + " access")
    if ok(base):
        return base
    for n in range(2, 100):
        cand = f"{clean[:34].strip()} {n}"
        if ok(cand):
            return cand
    return f"Access role {len(taken) + 1}"


def plan(templates: list[dict], assigned: dict[int, int], user_builtin: dict[int, set[str]],
         custom_roles: list[dict]) -> dict:
    """What to do with every template (PURE — the test runs it).

    templates:     [{id, name, role, is_active, tab_access, field_access, action_access}]
    assigned:      {user_id: template_id}
    user_builtin:  {user_id: {built-in role names}}
    custom_roles:  [{id, name, is_active, members: set(user_id)}]
    → {"builtin": {role: template_id}, "overwrite": {custom_role_id: template_id},
       "create": [{template_id, name, department, members: [user_id]}], "report": [str]}
    """
    by_template: dict[int, set[int]] = {}
    for uid, tid in assigned.items():
        by_template.setdefault(int(tid), set()).add(int(uid))
    roles_by_name = {str(r["name"]).strip().lower(): r for r in custom_roles if r.get("is_active", True)}
    taken = {str(r["name"]).strip().lower() for r in custom_roles}
    out = {"builtin": {}, "overwrite": {}, "create": [], "report": []}
    claimed_custom: set[int] = set()

    order = sorted(templates, key=lambda t: (-len(by_template.get(int(t["id"]), ())), int(t["id"])))
    for t in order:
        tid = int(t["id"])
        people = by_template.get(tid, set())
        if not t.get("is_active", True):
            if people:
                out["report"].append(f"'{t['name']}' was inactive — its {len(people)} user(s) follow their roles")
            continue
        if not people:
            out["report"].append(f"'{t['name']}' was assigned to nobody — not converted")
            continue
        if not (t.get("tab_access") or {}):
            out["report"].append(f"'{t['name']}' granted no tab — not converted")
            continue
        tag = str(t.get("role") or "").strip()
        rest = set(people)
        if tag in _CONFIGURABLE and tag not in out["builtin"]:
            covered = {u for u in people if tag in user_builtin.get(u, set())}
            if covered:
                out["builtin"][tag] = tid
                rest -= covered
                out["report"].append(
                    f"'{t['name']}' → the {tag} role's permissions ({len(covered)} user(s)); "
                    f"they now apply to everyone holding {tag}")
        elif tag.lower() in roles_by_name:
            role = roles_by_name[tag.lower()]
            members = set(role.get("members") or ())
            if members and members <= people and int(role["id"]) not in claimed_custom:
                claimed_custom.add(int(role["id"]))
                out["overwrite"][int(role["id"])] = tid
                rest -= members
                out["report"].append(f"'{t['name']}' → the '{role['name']}' role's permissions")
        if rest:
            name = role_name_for(t["name"], taken)
            taken.add(name.lower())
            dept = _DEPARTMENT.get(tag) or (roles_by_name.get(tag.lower()) or {}).get("department")
            out["create"].append({"template_id": tid, "name": name, "department": dept,
                                  "members": sorted(rest)})
            out["report"].append(f"'{t['name']}' → new role '{name}' for {len(rest)} user(s)")
    return out


def _cols(insp, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)} if insp.has_table(table) else set()


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    have = _cols(insp, "roles")
    for col in ("tab_access", "field_access", "action_access"):
        if col not in have:
            op.add_column("roles", sa.Column(col, sa.JSON(), nullable=True))

    if not (insp.has_table("access_templates") and "access_template_id" in _cols(insp, "user_profiles")):
        return
    t_cols = _cols(insp, "access_templates")
    templates = []
    for row in bind.execute(sa.text(
            "SELECT id, name, role, is_active, tab_access, field_access"
            + (", action_access" if "action_access" in t_cols else ", NULL")
            + " FROM access_templates")).all():
        templates.append({"id": row[0], "name": row[1], "role": row[2], "is_active": bool(row[3]),
                          "tab_access": _loads(row[4]) or {}, "field_access": _loads(row[5]) or {},
                          "action_access": _loads(row[6])})
    assigned = {int(u): int(t) for u, t in bind.execute(sa.text(
        "SELECT user_id, access_template_id FROM user_profiles WHERE access_template_id IS NOT NULL")).all()}
    user_builtin: dict[int, set[str]] = {}
    for uid, name in bind.execute(sa.text(
            "SELECT ur.user_id, r.name FROM user_roles ur JOIN roles r ON r.id = ur.role_id")).all():
        user_builtin.setdefault(int(uid), set()).add(str(name))
    custom_roles = []
    if insp.has_table("custom_roles"):
        members: dict[int, set[int]] = {}
        if insp.has_table("user_custom_roles"):
            for uid, rid in bind.execute(sa.text("SELECT user_id, custom_role_id FROM user_custom_roles")).all():
                members.setdefault(int(rid), set()).add(int(uid))
        dept_col = "department" if "department" in _cols(insp, "custom_roles") else "NULL"
        for rid, name, active, dept in bind.execute(sa.text(
                f"SELECT id, name, is_active, {dept_col} FROM custom_roles")).all():
            custom_roles.append({"id": int(rid), "name": name, "is_active": bool(active), "department": dept,
                                 "members": members.get(int(rid), set())})

    p = plan(templates, assigned, user_builtin, custom_roles)
    by_id = {int(t["id"]): t for t in templates}

    def grants(tid: int) -> dict:
        t = by_id[tid]
        acts = t.get("action_access")
        return {"t": json.dumps(t["tab_access"]), "f": json.dumps(t["field_access"] or {}),
                "a": None if acts is None else json.dumps(acts)}

    for role, tid in p["builtin"].items():
        g = grants(tid)
        bind.execute(sa.text("UPDATE roles SET tab_access = :t, field_access = :f, action_access = :a "
                             "WHERE name = :n"), {**g, "n": role})
    cr_cols = _cols(insp, "custom_roles")
    for rid, tid in p["overwrite"].items():
        g = grants(tid)
        if g["a"] is not None and "action_access" in cr_cols:
            bind.execute(sa.text("UPDATE custom_roles SET tab_access = :t, field_access = :f, action_access = :a "
                                 "WHERE id = :id"), {**g, "id": rid})
        else:
            bind.execute(sa.text("UPDATE custom_roles SET tab_access = :t, field_access = :f WHERE id = :id"),
                         {"t": g["t"], "f": g["f"], "id": rid})
    for item in p["create"]:
        g = grants(item["template_id"])
        t = by_id[item["template_id"]]
        cols = ["name", "description", "is_active", "tab_access", "field_access"]
        vals = {"name": item["name"],
                "description": (f"Moved from the Access Template '{t['name']}' (27 Oct 2026)")[:512],
                "is_active": True, "tab_access": g["t"], "field_access": g["f"]}
        if "action_access" in cr_cols:
            cols.append("action_access")
            vals["action_access"] = g["a"]
        if "department" in cr_cols and item.get("department"):
            cols.append("department")
            vals["department"] = item["department"]
        bind.execute(sa.text(f"INSERT INTO custom_roles ({', '.join(cols)}) "
                             f"VALUES ({', '.join(':' + c for c in cols)})"), vals)
        rid = bind.execute(sa.text("SELECT id FROM custom_roles WHERE name = :n"), {"n": item["name"]}).scalar()
        for uid in item["members"]:
            exists = bind.execute(sa.text(
                "SELECT 1 FROM user_custom_roles WHERE user_id = :u AND custom_role_id = :r"),
                {"u": uid, "r": rid}).first()
            if not exists:
                bind.execute(sa.text("INSERT INTO user_custom_roles (user_id, custom_role_id) VALUES (:u, :r)"),
                             {"u": uid, "r": rid})
    bind.execute(sa.text("UPDATE user_profiles SET access_template_id = NULL WHERE access_template_id IS NOT NULL"))
    for line in p["report"]:
        print(f"[0149] {line}")


def downgrade() -> None:
    # The assignments are not restored (the people now hold roles instead);
    # only the new columns go.
    insp = sa.inspect(op.get_bind())
    have = _cols(insp, "roles")
    for col in ("action_access", "field_access", "tab_access"):
        if col in have:
            op.drop_column("roles", col)
