"""po_project_allocations.contact_person_id — contact captured at allocation.

The PO form used to ask for a contact person up front, before any project was
chosen — so it was guesswork and usually left blank (26 Aug 2026, user
decision). The field moves to the moment it means something: allocating the PO
to a project. The PO-level column stays for legacy rows; new flows record the
contact per allocation.

Revision ID: 0085
Revises: 0084
"""
from alembic import op
import sqlalchemy as sa

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "po_project_allocations",
        sa.Column("contact_person_id", sa.Integer(),
                  sa.ForeignKey("contact_persons.id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("po_project_allocations", "contact_person_id")
