"""Navigator agent — discovery.

Responsibility: turn the configured categories into a de-duplicated list of
product detail URLs, each tagged with the category context it was found under.

Discovery strategy (recon-driven):
  category root  ->  configured subcategory listing pages  ->  product URLs

We traverse subcategories rather than paging the parent because robots.txt
disallows the ?p=/?page= pagination params. Subcategory listings on Safco are
server-rendered and small enough to fit a single page. Pagination / "load more"
handling is a documented scale item (see README) — the interface below already
returns a flat URL list, so adding a pager loop is localized.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..agents.classifier import extract_ldjson_blocks
from ..logging_setup import get_logger

log = get_logger("navigator")


@dataclass
class DiscoveredProduct:
    url: str
    category: str            # top-level, e.g. "Dental Exam Gloves"
    subcategory: str         # e.g. "nitrile-gloves"


class Navigator:
    def __init__(self, cfg, fetcher):
        self.cfg = cfg
        self.fetcher = fetcher

    def discover(self) -> list[DiscoveredProduct]:
        seen: set[str] = set()
        out: list[DiscoveredProduct] = []

        for cat in self.cfg.categories:
            for sub_path in cat.subcategories:
                sub_name = sub_path.rstrip("/").rsplit("/", 1)[-1]
                url = self.cfg.abs_url(sub_path)
                try:
                    result = self.fetcher.get(url, wait_selector="a.product-item-link")
                except Exception as e:
                    log.warning("subcategory_fetch_failed", url=url, error=str(e))
                    continue

                product_urls = self._extract_product_links(result.html)
                cap = self.cfg.crawl.max_products_per_subcategory
                if cap:
                    product_urls = product_urls[:cap]

                new = 0
                for purl in product_urls:
                    if purl in seen:
                        continue
                    seen.add(purl)
                    out.append(
                        DiscoveredProduct(
                            url=purl, category=cat.name, subcategory=sub_name
                        )
                    )
                    new += 1
                log.info("subcategory_discovered", subcategory=sub_name,
                         found=len(product_urls), new=new)

        log.info("discovery_complete", total_products=len(out))
        return out

    def _extract_product_links(self, html: str) -> list[str]:
        """Pull product detail URLs from a listing page, order-preserving.

        The visible product grid is JS-populated (anchor hrefs are empty in the
        server HTML), but Safco embeds a schema.org ItemList JSON-LD block that
        lists every product with its canonical URL. We read that — robust and
        browser-free — instead of scraping the rendered DOM.
        """
        urls: list[str] = []
        seen: set[str] = set()
        for block in extract_ldjson_blocks(html):
            if block.get("@type") != "ItemList":
                continue
            for el in block.get("itemListElement", []):
                if not isinstance(el, dict):
                    continue
                url = el.get("url") or (el.get("item") or {}).get("url")
                if not url or "/product/" not in url:
                    continue
                url = self.cfg.abs_url(url.split("?")[0].split("#")[0])
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
        return urls
