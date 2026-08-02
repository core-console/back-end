"""SQLAlchemy models for persisted users."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from core_console.database.base import Base


def _utc_now() -> datetime:
    """Return an aware UTC timestamp for ORM-managed changes."""

    return datetime.now(UTC)


class User(Base):
    """Persisted local representation of an externally identified user."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "regexp_replace(identity_issuer, '^[[:space:]]+|[[:space:]]+$', '', 'g') <> ''",
            name="ck_users_identity_issuer_not_blank",
        ),
        CheckConstraint(
            "regexp_replace(identity_subject, '^[[:space:]]+|[[:space:]]+$', '', 'g') <> ''",
            name="ck_users_identity_subject_not_blank",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_users_status",
        ),
        UniqueConstraint(
            "identity_issuer",
            "identity_subject",
            name="uq_users_identity_issuer_subject",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    identity_issuer: Mapped[str] = mapped_column(Text, nullable=False)
    identity_subject: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
    )
