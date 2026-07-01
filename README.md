# Frontier Dental — Agentic Product Catalog Scraper (POC)

A working prototype of an **agent-based scraping system** that discovers, extracts,
normalizes, and stores products from [Safco Dental Supply](https://www.safcodental.com),
scoped to two categories:

- **Dental Exam Gloves** — `/catalog/gloves`
- **Sutures & Surgical Products** — `/catalog/sutures-surgical-products`

The current run scrapes **53 products across both categories with 0 errors** and
100% field-fill on name, brand, SKU, price, availability, description, images, and
category path. See [`data/sample/`](data/sample/) for the output.

---

## TL;DR — run it

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# Deterministic run, both categories, no API key needed:
python -m src.main crawl --no-llm

# Full run with LLM, api key needed:
python -m src.main crawl

# Output: data/products.json, data/products.csv, data/run_summary.json
```

Quick smoke test (3 products/subcategory): `python -m src.main crawl --no-llm --limit 3`

---

## Architecture overview

```
                    ┌──────────────┐
   config.yaml ───► │ Orchestrator │ ◄─── CLI flags (typer)
                    └──────┬───────┘
                           │
        ┌──────────────────┼─────────────────────────────┐
        ▼                  ▼                              ▼
 ┌────────────┐    ┌──────────────┐               ┌──────────────┐
 │ Navigator  │    │  Fetcher     │  (shared)     │  Checkpoint  │
 │ discovery  │◄──►│ rate-limit + │               │ resumability │
 └─────┬──────┘    │ retry + robots               └──────────────┘
       │           │ httpx→browser│
       │ product   └──────┬───────┘
       │ URLs             │ HTML
       ▼                  ▼
 ┌────────────┐    ┌──────────────┐    ┌───────────┐    ┌──────────┐
 │ Classifier │──► │  Extractor   │──► │ Validator │──► │ Storage  │
 │ page type  │    │ rules + LLM  │    │  + dedup  │    │ SQLite + │
 └────────────┘    └──────────────┘    └───────────┘    │ JSON/CSV │
                          ▲                              └──────────┘
                   ┌──────┴───────┐
                   │ LLM fallback │  (optional, budgeted, gated)
                   │  Claude      │
                   └──────────────┘
```

Pipeline per run:

1. **Navigator** reads each configured subcategory listing and discovers product URLs.
2. For each URL (skipping ones already **checkpointed**):
   **Fetcher** gets HTML → **Classifier** confirms it's a product page →
   **Extractor** produces a normalized `Product` → **Validator** checks + dedups →
   **Storage** upserts it.
3. **Storage** exports JSON + CSV; a **run summary** with data-quality metrics is written.

---

## Why this approach

**Recon drove every decision.** Before writing extractors I inspected the live site.
Findings and the trade-offs they forced:

| Observation | Decision |
|---|---|
| Safco is **Magento**; both listing and detail pages ship a **schema.org JSON-LD** block | Extract from JSON-LD first — deterministic, stable, and free. No brittle CSS-position selectors. |
| Category listing page embeds an **`ItemList` JSON-LD** with every product URL + inline data | Discovery reads that block — **no headless browser needed** for the common path. |
| Product detail pages are **server-rendered** with a full `Product` + `BreadcrumbList` JSON-LD | httpx + parse. Fast, cheap, reliable. |
| Product **variant swatches, spec tables, and related products are AJAX-loaded** | Fetcher can **escalate to Playwright** (`render_mode`) when those fields are needed; documented as the hybrid boundary. |
| `robots.txt` **disallows `?p=`/`?page=`** pagination params | Traverse **subcategories** instead of paging the parent. Robots-compliant by construction. |

**AI is used where it earns its place, not as decoration.** The deterministic path
handles ~100% of core fields on these two categories, so the LLM is wired as a
*budgeted, gated fallback* for exactly the cases the brief calls out — page-type
detection on ambiguous pages, whole-page extraction when JSON-LD is missing
(irregular layouts), and parsing pack/unit size out of free-text descriptions.
A run with `--no-llm` produces the same core dataset at zero cost, which is the
honest demonstration that the AI is additive, not load-bearing.

---

## Agent responsibilities

| Agent | File | Responsibility | Uses LLM? |
|---|---|---|---|
| **Navigator** | [src/agents/navigator.py](src/agents/navigator.py) | Category → subcategory → product URLs (via `ItemList` JSON-LD), tagged with category context | No |
| **Classifier** | [src/agents/classifier.py](src/agents/classifier.py) | Label page as PRODUCT / LISTING / OTHER so the extractor isn't fed garbage | Fallback only, when rules are ambiguous |
| **Extractor** | [src/agents/extractor.py](src/agents/extractor.py) | Normalize a page into a `Product`: JSON-LD → microdata/DOM → regex → LLM | Fallback for irregular pages + pack size |
| **Validator** | [src/agents/validator.py](src/agents/validator.py) | Quality gate: reject bad records, dedup on `product_id`, emit fill-rate report | No |
| **Fetcher** | [src/fetcher.py](src/fetcher.py) | Single network choke point: rate limit, retry/backoff, robots, httpx↔browser | No |
| **Orchestrator** | [src/main.py](src/main.py) | Wire the agents, checkpointing, run summary, CLI | No |

Each agent has one job and a narrow interface, so any one can be swapped
(e.g. a queue-backed Navigator, a different LLM) without touching the others.

---

## Setup & execution

**Requirements:** Python 3.11+.

```bash
pip install -r requirements.txt

# Optional — only if you enable the LLM fallback:
cp .env.example .env          # then set ANTHROPIC_API_KEY
# Optional — only if you need variant/spec capture (render_mode: always/auto):
playwright install chromium
```

**Commands:**

```bash
python -m src.main crawl                      # config-driven full run
python -m src.main crawl --no-llm             # deterministic, no API key
python -m src.main crawl --limit 3            # quick test (3 products/subcat)
python -m src.main crawl --render always      # force browser (variants/specs)
python -m src.main crawl --fresh              # ignore checkpoint, re-crawl
python -m src.main export                      # re-export SQLite → JSON/CSV
```

Everything else (categories, delays, retries, model, budgets) is in
[config.yaml](config.yaml) — no behavior is hard-coded.

---

## Sample output schema

Full schema: [src/schema.py](src/schema.py). One row per product:

| Field | Type | Notes |
|---|---|---|
| `product_id` | string | SHA1(url)[:16] — primary key / dedup / idempotency key |
| `url` | string | Canonical product URL |
| `name` | string | Product name |
| `brand` | string? | Brand / manufacturer |
| `sku` | string? | SKU / item number |
| `category` | string | Top-level category |
| `category_path` | string[] | Full hierarchy, e.g. `["Dental Exam Gloves","Nitrile gloves"]` |
| `price` | float? | Base/lowest visible price |
| `currency` | string | Default `USD` |
| `pack_size` | string? | e.g. `"200 gloves per box"` |
| `availability` | enum | `in_stock` / `out_of_stock` / `backorder` / `unknown` |
| `description` | string? | |
| `specifications` | object | attribute → value (populated when rendered) |
| `image_urls` | string[] | |
| `variants` | object[] | size/option variants (populated when rendered) |
| `alternative_products` | string[] | related/upsell product URLs (populated when rendered) |
| `extraction_method` | enum | `rules` / `llm_fallback` / `partial` — provenance |
| `missing_fields` | string[] | which tracked fields were empty — data-quality signal |
| `scraped_at` | ISO datetime | |

Example (`data/sample/products.json`):

```json
{
  "product_id": "…",
  "url": "https://www.safcodental.com/product/compac-nitrile",
  "name": "Compac Nitrile",
  "brand": "Cranberry",
  "sku": "DRCDM",
  "category": "Dental Exam Gloves",
  "category_path": ["Dental Exam Gloves", "Nitrile gloves"],
  "price": 8.49,
  "currency": "USD",
  "pack_size": "100 gloves per box",
  "availability": "in_stock",
  "extraction_method": "rules",
  "missing_fields": []
}
```

---

## Failure handling

- **Retries:** every fetch is wrapped in exponential backoff (`tenacity`), retrying
  on 5xx / timeouts / transient network errors up to `crawl.max_retries`.
- **Rate limiting:** a single `RateLimiter` enforces `min_delay_seconds` between
  requests so we stay polite regardless of concurrency.
- **robots.txt:** parsed once; disallowed URLs are never fetched.
- **Isolation:** a failure on one product is logged and skipped — the run continues.
  Failed URLs are **not** checkpointed, so a later run automatically retries them.
- **Resumability:** completed URLs are appended to `.checkpoints/done.txt`; re-running
  after a crash/Ctrl-C skips finished work. `--fresh` forces a full re-crawl.
- **Idempotency:** storage upserts on `product_id`, so re-runs update in place — no
  duplicate rows.
- **Graceful degradation:** if the LLM is disabled, keyless, or over budget, the
  pipeline silently falls back to rules; a bad LLM response returns `None`, never
  crashes.
- **Observability:** structured JSON logs (`logs/crawl.log`) + a per-run
  `data/run_summary.json` with counts, errors, LLM calls, and field fill-rates.

---

## How I would scale to full-site crawling in production

The prototype is intentionally shaped so the interfaces don't change under scale:

1. **Decouple discovery from extraction with a queue.** Navigator publishes product
   URLs to a durable queue (SQS / Redis / Kafka); a pool of extractor workers
   consumes it. Enables horizontal scale and natural backpressure.
2. **Replace the flat-file checkpoint with a state table** (`url, status, attempts,
   last_error, content_hash`) in Postgres. Gives at-least-once processing, ret/dead-letter
   handling, and incremental re-crawls driven by sitemap `lastmod` (Safco's
   `products.xml` exposes it) — only re-scrape what changed.
3. **Full category coverage & pagination.** Extend the Navigator to walk the whole
   `catalog.xml` category tree and handle "load more"/rendered pagination via the
   Playwright path (robots-compliant, since we avoid `?p=` params).
4. **Politeness at scale:** per-domain token-bucket limiter, concurrency caps,
   randomized jitter, and a shared cache of `robots.txt` — coordinated across workers.
5. **Browser tier only where needed:** a separate, autoscaled Playwright worker pool
   for pages that require rendering (variants/specs), keeping the cheap httpx path
   as the default.
6. **Deployment path:** containerize (Dockerfile) → run as a scheduled job
   (Kubernetes CronJob / ECS Scheduled Task) → secrets from a manager (AWS Secrets
   Manager / Vault), never in config → outputs to S3 + a warehouse table.
7. **Cost control for AI:** the per-run `max_fallback_calls` budget becomes a global
   rate/spend budget; track LLM-fallback rate as a signal that selectors have drifted.

---

## How I would monitor data quality

The building blocks already exist in this POC; production wires them to alerting:

- **Fill-rate tracking (built):** `run_summary.json` reports per-field fill rates and
  each record carries `missing_fields`. Alert when a field's fill rate drops below a
  threshold (e.g. price < 95%) — the earliest signal that the site's markup changed.
- **Extraction provenance (built):** every record has `extraction_method`. A rising
  `llm_fallback` / `partial` share means deterministic selectors are breaking →
  trigger a **selector-repair** review (an LLM-assisted use the brief mentions).
- **Schema validation (built):** Pydantic rejects malformed records at construction;
  the Validator adds business rules (no name, negative price) and reports reject reasons.
- **Referential/volume checks:** compare product counts vs the sitemap's
  `numberOfItems` per category; alert on unexpected drops or spikes.
- **Drift & anomaly checks (prod):** track price distributions, dedup rates, and
  broken image/URL rates over time; flag statistical outliers.
- **Sampling & audit (prod):** periodically human-review a random sample and diff
  against previous crawls to catch silent quality regressions.

---

## Limitations (current)

- **Variants, specification tables, and related/alternative products are AJAX-loaded**
  and only populated when run with `--render always` (requires `playwright install
  chromium`). The default httpx run leaves these empty (recorded in `missing_fields`).
- **Pagination / "load more"** on large subcategories isn't traversed yet — discovery
  relies on the single-page `ItemList` JSON-LD. Sufficient for the two POC categories.
- **Concurrency is sequential** for POC clarity; the Fetcher interface supports a pool
  but it isn't wired.
- **`pack_size`** is regex-parsed from free text (~66% coverage); enabling the LLM
  fallback lifts this. Other categories may phrase pack sizes differently.
- Scoped to the **two configured categories**; extending is a config change, but other
  categories may present layouts that exercise the LLM fallback more.

---

## Project layout

```
config.yaml              # all runtime config (categories, delays, model, budgets)
requirements.txt
src/
  main.py                # orchestrator + CLI
  config.py  schema.py   # typed config + normalized product schema
  fetcher.py             # rate-limit + retry + robots + httpx↔browser
  storage.py             # SQLite + JSON/CSV export (idempotent upserts)
  checkpoint.py          # resumability
  logging_setup.py       # structured logging
  agents/                # navigator, classifier, extractor, validator
  llm/client.py          # budgeted, gated Claude fallback
data/sample/             # committed sample output (both categories)
```
