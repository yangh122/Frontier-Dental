"""Validator / deduplicator agent — the quality gate before persistence.

Responsibility:
  - reject records that fail minimum quality (no name, malformed price),
  - deduplicate within a run on product_id (hash of canonical URL),
  - surface a per-run data-quality summary used for observability/README.

Pydantic already enforces types at construction; this layer adds business rules
and cross-record dedup that a single record can't check for itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..logging_setup import get_logger
from ..schema import Product

log = get_logger("validator")


@dataclass
class QualityReport:
    total: int = 0
    accepted: int = 0
    rejected: int = 0
    duplicates: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    field_fill: dict[str, int] = field(default_factory=dict)

    def _bump(self, bucket: dict, key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1

    def as_dict(self) -> dict:
        fill_rate = {
            k: round(v / self.accepted, 3) if self.accepted else 0.0
            for k, v in self.field_fill.items()
        }
        return {
            "total_seen": self.total,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "reject_reasons": self.reasons,
            "field_fill_rate": fill_rate,
        }


class Validator:
    # Fields whose fill-rate we report on for data-quality monitoring.
    TRACKED = ["brand", "sku", "price", "availability", "description",
               "image_urls", "pack_size", "category_path"]

    def __init__(self):
        self._seen_ids: set[str] = set()
        self.report = QualityReport()

    def validate(self, product: Product) -> bool:
        """Return True if the product should be persisted."""
        self.report.total += 1

        # --- hard rules ---------------------------------------------------
        if not product.name or not product.name.strip():
            self.report._bump(self.report.reasons, "missing_name")
            self.report.rejected += 1
            return False
        if product.price is not None and product.price < 0:
            self.report._bump(self.report.reasons, "negative_price")
            self.report.rejected += 1
            return False

        # --- dedup --------------------------------------------------------
        if product.product_id in self._seen_ids:
            self.report.duplicates += 1
            log.info("duplicate_skipped", url=str(product.url))
            return False
        self._seen_ids.add(product.product_id)

        # --- accepted: record fill stats ---------------------------------
        self.report.accepted += 1
        for f in self.TRACKED:
            val = getattr(product, f)
            if val not in (None, "", [], {}) and str(val) != "unknown":
                self.report._bump(self.report.field_fill, f)
        return True
