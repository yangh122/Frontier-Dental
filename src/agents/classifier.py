"""Classifier agent — page-type detection.

Responsibility: label a fetched page as PRODUCT, LISTING, or OTHER so the
orchestrator routes it correctly. This guards the extractor against wasting an
LLM call (or emitting garbage) on an error/redirect/empty page.

Rules-first: JSON-LD @type and DOM signals settle the vast majority for free.
The LLM is consulted only when rules are genuinely ambiguous (no structured
data and mixed signals) — the "page type detection" use the brief calls out,
applied sparingly.
"""

from __future__ import annotations

import json
import re
from enum import Enum

from selectolax.parser import HTMLParser

from ..logging_setup import get_logger

log = get_logger("classifier")


class PageType(str, Enum):
    PRODUCT = "product"
    LISTING = "listing"
    OTHER = "other"


_LDJSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I
)


def extract_ldjson_blocks(html: str) -> list[dict]:
    blocks: list[dict] = []
    for raw in _LDJSON_RE.findall(html):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            blocks.extend(d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            blocks.append(data)
    return blocks


class Classifier:
    def __init__(self, cfg, llm=None):
        self.cfg = cfg
        self.llm = llm

    def classify(self, html: str) -> PageType:
        # --- rule 1: JSON-LD Product is a hard signal --------------------
        blocks = extract_ldjson_blocks(html)
        for b in blocks:
            if b.get("@type") == "Product":
                return PageType.PRODUCT

        tree = HTMLParser(html)

        # --- rule 2: DOM product-detail markers --------------------------
        if tree.css_first('[itemprop="sku"]') or tree.css_first(".product-info-main"):
            return PageType.PRODUCT

        # --- rule 3: listing grid ----------------------------------------
        tiles = tree.css("a.product-item-link")
        if len(tiles) >= 2:
            return PageType.LISTING

        # --- rule 4: ambiguous -> optional LLM fallback ------------------
        if self.llm and self.llm.available:
            verdict = self._llm_classify(tree)
            if verdict:
                log.info("llm_classified", verdict=verdict)
                return verdict

        return PageType.OTHER

    def _llm_classify(self, tree: HTMLParser) -> PageType | None:
        title = tree.css_first("title")
        h1 = tree.css_first("h1")
        snippet = " | ".join(
            t.text(strip=True) for t in (title, h1) if t is not None
        )[:500]
        data = self.llm.extract_json(
            system=(
                "You classify e-commerce pages. Respond ONLY with JSON: "
                '{"page_type": "product|listing|other"}.'
            ),
            user=f"Classify this page based on title/heading:\n{snippet}",
            max_tokens=64,
        )
        if not data:
            return None
        val = str(data.get("page_type", "")).lower()
        return {
            "product": PageType.PRODUCT,
            "listing": PageType.LISTING,
            "other": PageType.OTHER,
        }.get(val)
