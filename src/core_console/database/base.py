"""Shared SQLAlchemy metadata without fabricated models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for future real business models."""
