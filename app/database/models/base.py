"""
Base model and common mixins for all database tables.
Aligned with Procurement Bot Database Schema v2.0 (PostgreSQL 15+).

✅ ARCHITECTURE: Uses BIGINT IDENTITY primary keys for performance.
✅ MULTI-TENANCY: Relies on PostgreSQL Row-Level Security (RLS)
   via current_setting('app.company_id') and current_setting('app.is_supplier_owner').
✅ AUDIT: Timestamps managed by DB triggers (set_updated_at) and SQLAlchemy defaults.
"""
from datetime import datetime
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import Column, DateTime, BigInteger, func, Identity


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""
    pass


class IdentityMixin:
    """
    Adds a BIGINT IDENTITY primary key.
    Use for all tables that need a primary key (most of them).
    """
    id = Column(
        BigInteger,
        Identity(always=True),
        primary_key=True,
        comment="Primary Key - BIGINT IDENTITY"
    )


class TimestampMixin:
    """
    Adds created_at and updated_at timestamps.
    Use for tables that track creation and modification times.
    """
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Creation timestamp"
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="Last update timestamp"
    )


class AuditMixin:
    """
    Adds created_by and updated_by fields for tables that track user actions.
    Note: Foreign keys to users.id should be defined in the model.
    """
    created_by = Column(
        BigInteger,
        nullable=True,
        comment="User ID who created the record"
    )
    updated_by = Column(
        BigInteger,
        nullable=True,
        comment="User ID who last updated the record"
    )