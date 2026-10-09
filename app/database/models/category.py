"""
Category model - represents item categories.
Aligned with Procurement Bot Database Schema v2.0.

Supports hierarchical categories with self-referencing parent_id.
Global reference table shared across all suppliers.
"""
from sqlalchemy import Column, String, BigInteger, ForeignKey, CheckConstraint
from app.database.models.base import Base, IdentityMixin


class Category(Base, IdentityMixin):
    """
    Represents an item category.
    Supports hierarchical structure (parent-child) with self-referencing FK.
    """
    __tablename__ = "categories"

    # Parent category (self-referencing for hierarchy)
    parent_id = Column(
        BigInteger,
        ForeignKey("categories.id"),
        nullable=True,
        comment="Parent category ID (NULL for root categories)"
    )

    # Multilingual names
    name_ar = Column(
        String(500),
        nullable=True,
        comment="Arabic name"
    )
    name_ru = Column(
        String(500),
        nullable=True,
        comment="Russian name"
    )
    name_en = Column(
        String(500),
        nullable=True,
        comment="English name"
    )

    __table_args__ = (
        # Prevent self-referencing loop (a category cannot be its own parent)
        CheckConstraint(
            "parent_id IS NULL OR parent_id <> id",
            name="ck_categories_no_self_loop"
        ),
    )

    def __repr__(self):
        return f"<Category(id={self.id}, name_ar={self.name_ar})>"