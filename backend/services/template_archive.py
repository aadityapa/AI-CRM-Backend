"""Interview templates follow their opportunity (10 Oct 2026).

A position template is linked to one opportunity (`job_templates.opportunity_id`
holds the opportunity's code, e.g. "C-2026-00095"). When Sales closes the deal
(Won, Lost, Partial), rejects or archives it, the template moves to the
Templates page's **Archive** tab and cannot be used for a new AI interview;
when the deal is reopened the template is active again. Nothing is written —
the state is DERIVED from the opportunity's stage every time it is read, so the
two can never disagree.

Best-effort: without a CRM database (the legacy-only setup) nothing is archived.
"""
from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger("karnex.templates.archive")

#: Opportunity stages that put a template in the Archive tab. The two holds keep
#: it active — a hold is reversible and the position is still being worked on.
ARCHIVING_STAGES = frozenset({"Closed_Won", "Closed_Lost", "Closed_Partial", "Rejected", "Archived"})

STAGE_WORDS = {
    "Closed_Won": "Closed Won", "Closed_Lost": "Closed Lost", "Closed_Partial": "Closed Partial",
    "Rejected": "Rejected", "Archived": "Archived",
}


def _stage_value(stage) -> str:
    return str(getattr(stage, "value", stage) or "")


def is_archiving_stage(stage) -> bool:
    """PURE."""
    return _stage_value(stage) in ARCHIVING_STAGES


def opportunity_stages(opp_codes: Iterable[str]) -> dict[str, str]:
    """{opportunity code: pipeline stage} in ONE query. {} when the CRM is absent."""
    codes = sorted({str(c).strip() for c in opp_codes or [] if str(c or "").strip()})
    if not codes:
        return {}
    try:
        from sqlalchemy import select

        from crm_db import get_session_factory
        from models import Opportunity

        session = get_session_factory()()
        try:
            rows = session.execute(
                select(Opportunity.opp_id, Opportunity.pipeline_stage).where(Opportunity.opp_id.in_(codes))
            ).all()
        finally:
            session.close()
    except Exception as exc:  # no CRM configured, or a transient DB error
        logger.debug("template_archive.stages_unavailable: %s", exc)
        return {}
    return {str(code): _stage_value(stage) for code, stage in rows}


def annotate(jobs: list[dict]) -> list[dict]:
    """Stamp `archived`, `opportunity_stage` and `archived_reason` on each template."""
    stages = opportunity_stages(str((j or {}).get("opportunityId") or "") for j in jobs or [])
    for job in jobs or []:
        code = str((job or {}).get("opportunityId") or "").strip()
        stage = stages.get(code, "")
        job["opportunity_stage"] = stage or None
        job["archived"] = bool(stage) and is_archiving_stage(stage)
        job["archived_reason"] = (
            f"Opportunity {code} is {STAGE_WORDS.get(stage, stage)}" if job["archived"] else None
        )
    return jobs


def archived_reason(job: dict | None) -> str | None:
    """Why this ONE template cannot be used now, or None."""
    if not job:
        return None
    annotate([job])
    return job.get("archived_reason")
