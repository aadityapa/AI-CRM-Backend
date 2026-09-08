"""Generic CRUD service for CRM master-data resources.

One small factory (MasterResource + four functions) drives all seven master
tables so routers/crm/masters.py does not repeat itself 7x.
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from crm_deps import PageParams
from services.crm_common import paginate


@dataclass(frozen=True)
class MasterResource:
    """Configuration for one master table exposed via the generic CRUD."""

    model: type
    label: str                      # human name for messages, e.g. "Department"
    search_columns: tuple           # ORM columns matched by ?search= (ILIKE)
    has_is_active: bool = False     # whether the table supports ?is_active=


def list_masters(db: Session, res: MasterResource, p: PageParams,
                 is_active: bool | None = None) -> tuple[list, dict]:
    stmt = select(res.model)
    if p.search:
        like = f"%{p.search}%"
        stmt = stmt.where(sa.or_(*[col.ilike(like) for col in res.search_columns]))
    if is_active is not None and res.has_is_active:
        stmt = stmt.where(res.model.is_active == is_active)
    stmt = stmt.order_by(res.model.id.asc())
    return paginate(db, stmt, p.page, p.limit)


def get_master(db: Session, res: MasterResource, item_id: int):
    obj = db.get(res.model, item_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{res.label} not found")
    return obj


def create_master(db: Session, res: MasterResource, data: dict):
    obj = res.model(**data)
    db.add(obj)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"{res.label} conflicts with an existing row (duplicate value or invalid reference)",
        )
    db.refresh(obj)
    return obj


def update_master(db: Session, res: MasterResource, item_id: int, data: dict):
    obj = get_master(db, res, item_id)
    for field, value in data.items():
        setattr(obj, field, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"{res.label} conflicts with an existing row (duplicate value or invalid reference)",
        )
    db.refresh(obj)
    return obj
