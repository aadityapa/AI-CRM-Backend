"""Repair interview_events written by two different importers.

Symptom: the Interviews tab shows the same round twice — once as a plain block of
text ("Round: L1-Interview / Stage: RMG / ... / <feedback>") and once as a proper
card, with the feedback repeated in both.

Cause: `import_candidate_profiles.py` flattened every round into `note` and
de-duplicated on (kind, scheduled_at). `import_applied_opportunities.py` writes
structured columns and originally de-duplicated on (kind, scheduled_at, stage) —
since legacy rows have no stage, the keys never matched and a second row was
inserted for every round that already existed.

This script fixes rows already in the database:

  1. Groups interview_events by (profile_id, kind, scheduled_at).
  2. Keeps the richest row, merges any structured field the others have, deletes
     the duplicates.
  3. Parses legacy "Round:/Stage:/Mode:/Status:/Result:" notes into the real
     columns, then clears the note so the feedback renders once.
  4. Rewrites interviewer values saved as a raw Python list repr
     ("[{'id': '270...', 'text': 'Mohit Arya'}]") to just the name.

The importer itself is fixed too, so this is a one-time repair — re-running the
import will not re-create duplicates.

Dry run by default.

Usage:
    python scripts/dedupe_interview_events.py            # report only
    python scripts/dedupe_interview_events.py --apply    # write changes
    python scripts/dedupe_interview_events.py --profile 15920   # limit to one profile
"""
from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ on sys.path

from sqlalchemy import select

from crm_db import get_session_factory
from models import InterviewEvent

#: "Round: X", "Stage: Y", ... as written by import_candidate_profiles.py.
LABEL_RE = re.compile(
    r"^(Round|Stage|Mode|Status|Result)\s*:\s*(.+)$", re.IGNORECASE)
LABEL_TO_COL = {"stage": "stage", "mode": "mode", "status": "status", "result": "result"}
#: Structured columns, richest-first — used to score which duplicate to keep.
FIELDS = ("stage", "mode", "status", "result", "interviewer", "feedback",
          "meeting_link", "raw_when", "zoho_round_id")


def squash(s) -> str:
    return " ".join(str(s or "").split())


def parse_legacy_note(note: str | None) -> tuple[dict, str | None]:
    """Split an old flattened note into ({column: value}, leftover_free_text).

    Only the leading "Label: value" lines are consumed; everything after them is
    the interviewer's feedback, which moves into the `feedback` column.
    """
    raw = (note or "").strip()
    if not raw:
        return {}, None
    fields: dict[str, str] = {}
    rest: list[str] = []
    consuming = True
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = LABEL_RE.match(line) if consuming else None
        if m:
            label, value = m.group(1).lower(), squash(m.group(2))
            col = LABEL_TO_COL.get(label)
            if col and value:
                fields[col] = value
            continue
        consuming = False
        rest.append(line)
    return fields, ("\n".join(rest).strip() or None)


def clean_people(value: str | None) -> str | None:
    """"[{'id': '270...', 'text': 'Mohit Arya'}]" -> "Mohit Arya"."""
    raw = (value or "").strip()
    if not raw.startswith("[") and not raw.startswith("{"):
        return value
    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return value
    items = parsed if isinstance(parsed, (list, tuple)) else [parsed]
    names = []
    for item in items:
        if isinstance(item, dict):
            name = squash(item.get("text") or item.get("name"))
        else:
            name = squash(item)
        if name:
            names.append(name)
    return ", ".join(dict.fromkeys(names)) or None


def richness(ev: InterviewEvent) -> tuple[int, int]:
    """Prefer the row with more structured columns filled, then the newer id."""
    filled = sum(1 for f in FIELDS if getattr(ev, f, None))
    return (filled, ev.id or 0)


def main() -> int:
    argv = sys.argv[1:]
    apply = "--apply" in argv
    profile_id = None
    if "--profile" in argv:
        i = argv.index("--profile")
        if i + 1 < len(argv):
            profile_id = int(argv[i + 1])

    db = get_session_factory()()
    stats = dict(groups=0, deleted=0, merged=0, notes_parsed=0, people_fixed=0, scanned=0)
    try:
        stmt = select(InterviewEvent).order_by(InterviewEvent.id)
        if profile_id is not None:
            stmt = stmt.where(InterviewEvent.profile_id == profile_id)
        rows = db.execute(stmt).scalars().all()
        stats["scanned"] = len(rows)
        print(f"Scanning {len(rows):,} interview_events"
              f"{f' for profile {profile_id}' if profile_id else ''}")
        print(f"Mode: {'APPLY' if apply else 'DRY RUN'}\n")

        groups: dict[tuple, list[InterviewEvent]] = defaultdict(list)
        for ev in rows:
            groups[(ev.profile_id, ev.kind, ev.scheduled_at)].append(ev)

        examples: list[str] = []
        for key, evs in groups.items():
            # ---- 1. collapse duplicates ----------------------------------
            keep = max(evs, key=richness)
            for other in evs:
                if other is keep:
                    continue
                for f in FIELDS:
                    val = getattr(other, f, None)
                    if val and not getattr(keep, f, None):
                        setattr(keep, f, val)
                # A note worth keeping is one the winner doesn't already have.
                if other.note and not keep.note:
                    keep.note = other.note
                db.delete(other)
                stats["deleted"] += 1
            if len(evs) > 1:
                stats["groups"] += 1
                if len(examples) < 5:
                    examples.append(
                        f"  profile {key[0]} · {key[1]} · {key[2]} — "
                        f"{len(evs)} rows -> kept #{keep.id}")

            # ---- 2. promote legacy note text into columns ----------------
            fields, leftover = parse_legacy_note(keep.note)
            if fields or (leftover and not keep.feedback):
                for col, val in fields.items():
                    if not getattr(keep, col, None):
                        setattr(keep, col, val[:120] if col == "stage" else val[:60])
                if leftover and not keep.feedback:
                    keep.feedback = leftover
                # The note was only ever a rendering of these columns — drop it so
                # the feedback is not shown twice.
                if fields:
                    keep.note = None
                    stats["notes_parsed"] += 1
            elif keep.note and keep.feedback and squash(keep.feedback) in squash(keep.note):
                keep.note = None
                stats["notes_parsed"] += 1

            # ---- 3. de-repr the interviewer ------------------------------
            fixed = clean_people(keep.interviewer)
            if fixed != keep.interviewer:
                keep.interviewer = fixed[:200] if fixed else None
                stats["people_fixed"] += 1

            stats["merged"] += 1

        if examples:
            print("Duplicate groups (first 5):")
            print("\n".join(examples), "\n")

        print(f"Duplicate groups found : {stats['groups']:,}")
        print(f"Rows to delete         : {stats['deleted']:,}")
        print(f"Legacy notes parsed    : {stats['notes_parsed']:,}")
        print(f"Interviewer values fixed: {stats['people_fixed']:,}")
        print(f"Rows remaining         : {stats['scanned'] - stats['deleted']:,}")

        if apply:
            db.commit()
            print("\nCOMMITTED.")
        else:
            db.rollback()
            print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
