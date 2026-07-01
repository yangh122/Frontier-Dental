"""Normalized product schema.

This is the contract every extractor must satisfy, regardless of whether the
data came from deterministic CSS selectors or the LLM fallback. Documented once
here so the output format is unambiguous (README references this module).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


class Availability(str, Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    BACKORDER = "backorder"
    UNKNOWN = "unknown"


class ExtractionMethod(str, Enum):
    """Provenance — how each record was extracted. Kept on every row so we can
    measure LLM-fallback rate and audit data quality."""

    RULES = "rules"          # deterministic CSS/DOM selectors
    LLM_FALLBACK = "llm_fallback"  # selectors failed, LLM rescued it
    PARTIAL = "partial"      # some fields rules, some LLM


class ProductVariant(BaseModel):
    """A purchasable variation of a product (e.g. glove size S/M/L, each its own
    SKU and pack). Sutures/gloves both sell as size- or gauge-keyed variants."""

    sku: Optional[str] = None
    option_label: Optional[str] = Field(
        None, description="e.g. 'Small', 'Medium', '3-0 / 18in'"
    )
    price: Optional[float] = None
    pack_size: Optional[str] = Field(None, description="e.g. '100/box', '12/pk'")
    availability: Availability = Availability.UNKNOWN


class Product(BaseModel):
    """One product/detail page, normalized."""

    # --- Identity ---------------------------------------------------------
    product_id: str = Field(
        ..., description="Stable hash of the product URL; primary key / dedup key"
    )
    url: HttpUrl
    name: str
    brand: Optional[str] = Field(None, description="Brand / manufacturer")
    sku: Optional[str] = Field(None, description="Top-level SKU / item number")

    # --- Taxonomy ---------------------------------------------------------
    category: str = Field(..., description="Top category, e.g. 'Dental Exam Gloves'")
    category_path: list[str] = Field(
        default_factory=list,
        description="Full hierarchy, e.g. ['Gloves', 'Nitrile Gloves']",
    )

    # --- Commercial -------------------------------------------------------
    price: Optional[float] = Field(None, description="Base/lowest price if visible")
    currency: str = "USD"
    pack_size: Optional[str] = None
    availability: Availability = Availability.UNKNOWN

    # --- Content ----------------------------------------------------------
    description: Optional[str] = None
    specifications: dict[str, str] = Field(
        default_factory=dict, description="Attribute name -> value"
    )
    image_urls: list[str] = Field(default_factory=list)
    variants: list[ProductVariant] = Field(default_factory=list)
    alternative_products: list[str] = Field(
        default_factory=list, description="URLs of related/alternative products"
    )

    # --- Provenance / audit ----------------------------------------------
    extraction_method: ExtractionMethod = ExtractionMethod.RULES
    missing_fields: list[str] = Field(default_factory=list)
    scraped_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @field_validator("price")
    @classmethod
    def _non_negative_price(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v < 0:
            raise ValueError("price cannot be negative")
        return v

    @staticmethod
    def make_id(url: str) -> str:
        """Deterministic id from canonical URL -> idempotent upserts + dedup."""
        return hashlib.sha1(url.strip().lower().encode()).hexdigest()[:16]
