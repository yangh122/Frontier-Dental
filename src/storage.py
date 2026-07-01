"""Persistence layer: SQLite as the queryable store + JSON/CSV exporters.

Upserts are keyed on product_id (hash of URL) so re-running the crawl is
idempotent — a product seen twice updates in place rather than duplicating.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .schema import Product


class Storage:
    def __init__(self, sqlite_path: str):
        self.path = Path(sqlite_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                product_id        TEXT PRIMARY KEY,
                url               TEXT NOT NULL,
                name              TEXT,
                brand             TEXT,
                sku               TEXT,
                category          TEXT,
                category_path     TEXT,   -- JSON array
                price             REAL,
                currency          TEXT,
                pack_size         TEXT,
                availability      TEXT,
                description       TEXT,
                specifications    TEXT,   -- JSON object
                image_urls        TEXT,   -- JSON array
                variants          TEXT,   -- JSON array
                alternative_products TEXT,-- JSON array
                extraction_method TEXT,
                missing_fields    TEXT,   -- JSON array
                scraped_at        TEXT
            )
            """
        )
        self.conn.commit()

    def upsert(self, product: Product) -> None:
        """Idempotent write keyed on product_id."""
        d = json.loads(product.model_dump_json())
        self.conn.execute(
            """
            INSERT INTO products (
                product_id, url, name, brand, sku, category, category_path,
                price, currency, pack_size, availability, description,
                specifications, image_urls, variants, alternative_products,
                extraction_method, missing_fields, scraped_at
            ) VALUES (
                :product_id, :url, :name, :brand, :sku, :category, :category_path,
                :price, :currency, :pack_size, :availability, :description,
                :specifications, :image_urls, :variants, :alternative_products,
                :extraction_method, :missing_fields, :scraped_at
            )
            ON CONFLICT(product_id) DO UPDATE SET
                name=excluded.name, brand=excluded.brand, sku=excluded.sku,
                category=excluded.category, category_path=excluded.category_path,
                price=excluded.price, pack_size=excluded.pack_size,
                availability=excluded.availability, description=excluded.description,
                specifications=excluded.specifications, image_urls=excluded.image_urls,
                variants=excluded.variants,
                alternative_products=excluded.alternative_products,
                extraction_method=excluded.extraction_method,
                missing_fields=excluded.missing_fields, scraped_at=excluded.scraped_at
            """,
            {
                "product_id": d["product_id"],
                "url": d["url"],
                "name": d["name"],
                "brand": d["brand"],
                "sku": d["sku"],
                "category": d["category"],
                "category_path": json.dumps(d["category_path"]),
                "price": d["price"],
                "currency": d["currency"],
                "pack_size": d["pack_size"],
                "availability": d["availability"],
                "description": d["description"],
                "specifications": json.dumps(d["specifications"]),
                "image_urls": json.dumps(d["image_urls"]),
                "variants": json.dumps(d["variants"]),
                "alternative_products": json.dumps(d["alternative_products"]),
                "extraction_method": d["extraction_method"],
                "missing_fields": json.dumps(d["missing_fields"]),
                "scraped_at": d["scraped_at"],
            },
        )
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    def all_rows(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM products").fetchall()
        return [dict(r) for r in rows]

    # --- Exporters --------------------------------------------------------
    def export(self, export_dir: str, formats: Iterable[str]) -> list[str]:
        Path(export_dir).mkdir(parents=True, exist_ok=True)
        written = []
        rows = self.all_rows()
        if "json" in formats:
            written.append(self._export_json(export_dir, rows))
        if "csv" in formats:
            written.append(self._export_csv(export_dir, rows))
        return written

    def _export_json(self, export_dir: str, rows: list[dict]) -> str:
        # Re-hydrate JSON columns so nested structures export as real objects.
        out = []
        for r in rows:
            r = dict(r)
            for col in (
                "category_path", "specifications", "image_urls",
                "variants", "alternative_products", "missing_fields",
            ):
                r[col] = json.loads(r[col]) if r[col] else ([] if col != "specifications" else {})
            out.append(r)
        path = str(Path(export_dir) / "products.json")
        Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")
        return path

    def _export_csv(self, export_dir: str, rows: list[dict]) -> str:
        # Flat CSV: nested fields stay JSON-encoded strings in their cells.
        path = str(Path(export_dir) / "products.csv")
        if not rows:
            Path(path).write_text("", encoding="utf-8")
            return path
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def close(self) -> None:
        self.conn.close()
