"""Extractor agent — turn a product page into a normalized Product.

Layered strategy (rules-first, LLM last):

  1. JSON-LD Product + BreadcrumbList  -> name, brand, sku, price, currency,
     availability, images, description, category_path. Safco embeds these in
     server-rendered HTML, so the common path is deterministic and free.
  2. Microdata / DOM  -> price range (low/high), specifications, variants,
     alternative products. Some of these are JS-loaded and only present when the
     page was fetched with render=True (see Fetcher); absent otherwise.
  3. Regex over description  -> pack/unit size ("200 gloves per box").
  4. LLM fallback  -> only when JSON-LD is missing/broken (irregular pages) or
     pack size couldn't be parsed. This is the "extraction fallback for
     irregular layouts" the brief calls for — used sparingly, budgeted, logged.

Every Product records its extraction_method and missing_fields so data quality
is measurable downstream.
"""

from __future__ import annotations

import re
from typing import Optional

from selectolax.parser import HTMLParser

from ..logging_setup import get_logger
from ..schema import (
    Availability,
    ExtractionMethod,
    Product,
    ProductVariant,
)
from .classifier import extract_ldjson_blocks

log = get_logger("extractor")

_AVAILABILITY_MAP = {
    "instock": Availability.IN_STOCK,
    "in_stock": Availability.IN_STOCK,
    "outofstock": Availability.OUT_OF_STOCK,
    "backorder": Availability.BACKORDER,
    "preorder": Availability.BACKORDER,
}

# "200 gloves per box", "100/box", "12/pk", "case of 10"
_PACK_RE = re.compile(
    r"(\d+\s*(?:gloves?|units?|pcs?|pieces?)?\s*(?:per|/)\s*(?:box|bx|pk|pack|case)"
    r"|\d+\s*/\s*(?:box|bx|pk|pack|case)"
    r"|case of \d+)",
    re.I,
)

# Fields we care about for data-quality accounting.
_TRACKED_FIELDS = [
    "name", "brand", "sku", "price", "availability",
    "description", "image_urls", "category_path",
]


class Extractor:
    def __init__(self, cfg, llm=None):
        self.cfg = cfg
        self.llm = llm

    def extract(self, url: str, html: str, category: str,
                subcategory: str) -> Optional[Product]:
        blocks = extract_ldjson_blocks(html)
        product_ld = next(
            (b for b in blocks if b.get("@type") == "Product"), None
        )
        breadcrumb_ld = next(
            (b for b in blocks if b.get("@type") == "BreadcrumbList"), None
        )
        tree = HTMLParser(html)

        method = ExtractionMethod.RULES
        if product_ld:
            fields = self._from_jsonld(product_ld)
        else:
            # No structured data -> irregular page. Try the LLM fallback.
            fields = self._from_llm(url, tree) or {}
            method = (
                ExtractionMethod.LLM_FALLBACK if fields else ExtractionMethod.RULES
            )

        if not fields.get("name"):
            log.warning("extract_no_name", url=url)
            return None

        # --- taxonomy: prefer breadcrumb, fall back to config context -----
        category_path = self._category_path(breadcrumb_ld) or [category, subcategory]

        # --- DOM-only extras (present mainly when rendered) ---------------
        specifications = self._specifications(tree)
        variants = self._variants(tree)
        alternatives = self._alternatives(tree)

        # --- pack size: regex over description, LLM only if needed --------
        pack_size = self._pack_size(fields.get("description"))
        if pack_size is None and fields.get("description") and self.llm \
                and self.llm.available and method == ExtractionMethod.RULES:
            pack_size = self._pack_size_llm(fields["description"])
            if pack_size:
                method = ExtractionMethod.PARTIAL

        product = Product(
            product_id=Product.make_id(url),
            url=url,
            name=fields["name"],
            brand=fields.get("brand"),
            sku=fields.get("sku"),
            category=category,
            category_path=category_path,
            price=fields.get("price"),
            currency=fields.get("currency", "USD"),
            pack_size=pack_size,
            availability=fields.get("availability", Availability.UNKNOWN),
            description=fields.get("description"),
            specifications=specifications,
            image_urls=fields.get("image_urls", []),
            variants=variants,
            alternative_products=alternatives,
            extraction_method=method,
        )
        product.missing_fields = self._missing(product)
        log.info("extracted", url=url, method=method.value,
                 missing=len(product.missing_fields))
        return product

    # --- JSON-LD ----------------------------------------------------------
    def _from_jsonld(self, ld: dict) -> dict:
        out: dict = {}
        out["name"] = _clean(ld.get("name"))
        out["description"] = _clean(ld.get("description"))
        out["sku"] = _clean(ld.get("sku"))

        brand = ld.get("brand")
        if isinstance(brand, dict):
            out["brand"] = _clean(brand.get("name"))
        elif isinstance(brand, str):
            out["brand"] = _clean(brand)

        img = ld.get("image")
        if isinstance(img, str):
            out["image_urls"] = [img]
        elif isinstance(img, list):
            out["image_urls"] = [i for i in img if isinstance(i, str)]

        offers = ld.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if isinstance(offers, dict):
            price = offers.get("price") or offers.get("lowPrice")
            out["price"] = _to_float(price)
            out["currency"] = offers.get("priceCurrency", "USD")
            out["availability"] = _map_availability(offers.get("availability"))
        return out

    def _category_path(self, breadcrumb: Optional[dict]) -> list[str]:
        if not breadcrumb:
            return []
        items = breadcrumb.get("itemListElement", [])
        names = [i.get("name") for i in items if isinstance(i, dict) and i.get("name")]
        # Drop leading nav crumbs (Home / Dental Supplies) and the product itself.
        drop = {"home", "dental supplies"}
        names = [n for n in names if n.strip().lower() not in drop]
        return names[:-1] if len(names) > 1 else names

    # --- DOM extras -------------------------------------------------------
    def _specifications(self, tree: HTMLParser) -> dict[str, str]:
        specs: dict[str, str] = {}
        # Magento "Additional Information" table: rows of th(label)/td(value).
        for row in tree.css("table.additional-attributes tr, .additional-attributes tr"):
            th = row.css_first("th")
            td = row.css_first("td")
            if th and td:
                k, v = th.text(strip=True), td.text(strip=True)
                if k and v:
                    specs[k] = v
        return specs

    def _variants(self, tree: HTMLParser) -> list[ProductVariant]:
        variants: list[ProductVariant] = []
        for opt in tree.css(".swatch-option, .product-options-wrapper option"):
            label = (opt.attributes.get("data-option-label")
                     or opt.attributes.get("aria-label")
                     or opt.text(strip=True))
            if label and label.lower() not in ("choose an option", ""):
                variants.append(ProductVariant(option_label=label.strip()))
        return variants

    def _alternatives(self, tree: HTMLParser) -> list[str]:
        urls, seen = [], set()
        for a in tree.css(".upsell a, .related a, .crosssell a"):
            href = a.attributes.get("href", "")
            if "/product/" in href:
                u = self.cfg.abs_url(href.split("?")[0].split("#")[0])
                if u not in seen:
                    seen.add(u)
                    urls.append(u)
        return urls

    # --- pack size --------------------------------------------------------
    def _pack_size(self, description: Optional[str]) -> Optional[str]:
        if not description:
            return None
        m = _PACK_RE.search(description)
        return m.group(0).strip() if m else None

    def _pack_size_llm(self, description: str) -> Optional[str]:
        data = self.llm.extract_json(
            system=(
                "Extract the pack/unit size from a product description. Respond "
                'ONLY with JSON: {"pack_size": "<e.g. 200/box>"} or '
                '{"pack_size": null} if none is stated.'
            ),
            user=description[:1500],
            max_tokens=64,
        )
        if data and data.get("pack_size"):
            return str(data["pack_size"]).strip()
        return None

    # --- whole-page LLM fallback (irregular pages) -----------------------
    def _from_llm(self, url: str, tree: HTMLParser) -> Optional[dict]:
        if not (self.llm and self.llm.available):
            return None
        body = tree.css_first("body")
        text = (body.text(separator=" ", strip=True) if body else "")[:6000]
        data = self.llm.extract_json(
            system=(
                "Extract product fields from raw page text. Respond ONLY with "
                "JSON keys: name, brand, sku, price (number), currency, "
                "description. Use null when a field is absent."
            ),
            user=f"URL: {url}\n\nPAGE TEXT:\n{text}",
            max_tokens=800,
        )
        if not data or not data.get("name"):
            return None
        return {
            "name": _clean(data.get("name")),
            "brand": _clean(data.get("brand")),
            "sku": _clean(data.get("sku")),
            "price": _to_float(data.get("price")),
            "currency": data.get("currency") or "USD",
            "description": _clean(data.get("description")),
            "availability": Availability.UNKNOWN,
            "image_urls": [],
        }

    # --- data-quality accounting -----------------------------------------
    def _missing(self, p: Product) -> list[str]:
        missing = []
        for f in _TRACKED_FIELDS:
            val = getattr(p, f)
            if val in (None, "", [], {}) or val == Availability.UNKNOWN:
                missing.append(f)
        return missing


# --- module helpers -------------------------------------------------------
def _clean(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def _map_availability(v) -> Availability:
    if not v:
        return Availability.UNKNOWN
    key = str(v).rsplit("/", 1)[-1].replace(" ", "").lower()
    return _AVAILABILITY_MAP.get(key, Availability.UNKNOWN)
