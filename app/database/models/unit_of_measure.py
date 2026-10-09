"""
UnitOfMeasure model - represents units of measurement.
Aligned with Procurement Bot Database Schema v2.0.

Reference table with multilingual names.
Seeded with initial data (pcs, kg, m, l, box).
"""
from sqlalchemy import Column, String
from app.database.models.base import Base, IdentityMixin


class UnitOfMeasure(Base, IdentityMixin):
    """
    Represents a unit of measurement (e.g., piece, kilogram, meter).
    This is a global reference table shared across all companies and suppliers.
    """
    __tablename__ = "units_of_measure"

    # Unique unit code (e.g., "pcs", "kg", "m", "l", "box")
    code = Column(
        String(20),
        unique=True,
        nullable=False,
        comment="Unit code (pcs, kg, m, l, box)"
    )

    # Multilingual names
    name_ar = Column(
        String(200),
        nullable=True,
        comment="Arabic name"
    )
    name_ru = Column(
        String(200),
        nullable=True,
        comment="Russian name"
    )
    name_en = Column(
        String(200),
        nullable=True,
        comment="English name"
    )

    def __repr__(self):
        return f"<UnitOfMeasure(id={self.id}, code={self.code})>"