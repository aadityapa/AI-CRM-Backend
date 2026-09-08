r"""Seed the six per-role default Access Templates (Phase C, 27 Aug 2026).

    cd backend
    python scripts/seed_role_templates.py              # create missing only
    python scripts/seed_role_templates.py --overwrite  # reset all six to defaults

Creates "Default — Sales", "Default — Sales Head", "Default — RMG",
"Default — TA", "Default — HR", "Default — Finance" from the agreed role
matrix. Existing templates with those names are left untouched unless
--overwrite. Role-tagged, so new users of each role auto-assign.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--overwrite", action="store_true",
                    help="Reset the six templates to the defaults even if customised")
    args = ap.parse_args()

    from crm_db import get_session_factory
    from services.role_template_defaults import seed_role_templates

    db = get_session_factory()()
    try:
        result = seed_role_templates(db, overwrite=args.overwrite)
        db.commit()
        for role, what in result.items():
            print(f"  {role:<11} {what}")
        print("Done. Assign them in Access Control → Access Templates, or let "
              "auto-assign pick them up for new users.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
