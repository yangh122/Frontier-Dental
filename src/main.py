"""Orchestrator + CLI entrypoint.

Ties the agents into one pipeline:

    Navigator.discover()  ->  [product URLs]
        for each (skipping checkpointed):
            Fetcher.get() -> Classifier.classify() -> Extractor.extract()
            -> Validator.validate() -> Storage.upsert() -> Checkpoint.mark()
    Storage.export() + write run summary

Everything is config-driven; CLI flags are thin overrides for common runs.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import typer

from .agents.classifier import Classifier, PageType
from .agents.extractor import Extractor
from .agents.navigator import Navigator
from .agents.validator import Validator
from .checkpoint import Checkpoint
from .config import Config
from .fetcher import Fetcher
from .llm.client import LLMClient
from .logging_setup import get_logger, setup_logging
from .storage import Storage

app = typer.Typer(add_completion=False, help="Frontier Dental scraping POC")


@app.command()
def crawl(
    config: str = typer.Option("config.yaml", help="Path to config file"),
    limit: Optional[int] = typer.Option(
        None, help="Cap products per subcategory (quick test runs)"
    ),
    render: Optional[str] = typer.Option(
        None, help="Override render_mode: auto | always | never"
    ),
    no_llm: bool = typer.Option(False, help="Disable LLM fallback for this run"),
    fresh: bool = typer.Option(False, help="Ignore checkpoint and re-crawl all"),
):
    """Run the full discovery -> extraction -> storage pipeline."""
    cfg = Config.load(config)
    if limit is not None:
        cfg.crawl.max_products_per_subcategory = limit
    if no_llm:
        cfg.llm.enabled = False
    if render is not None:
        cfg.crawl.render_mode = render

    setup_logging(cfg.logging.level, cfg.logging.file)
    log = get_logger("orchestrator")

    checkpoint = Checkpoint()
    if fresh:
        Path(".checkpoints/done.txt").unlink(missing_ok=True)
        checkpoint = Checkpoint()

    llm = LLMClient(cfg)
    storage = Storage(cfg.storage.sqlite_path)
    validator = Validator()
    started = time.time()

    log.info("run_start", categories=[c.name for c in cfg.categories],
             llm_available=llm.available, render_mode=cfg.crawl.render_mode)

    with Fetcher(cfg) as fetcher:
        navigator = Navigator(cfg, fetcher)
        classifier = Classifier(cfg, llm)
        extractor = Extractor(cfg, llm)

        discovered = navigator.discover()
        errors = 0

        for dp in discovered:
            if checkpoint.is_done(dp.url):
                continue
            try:
                result = fetcher.get(dp.url)
                page_type = classifier.classify(result.html)
                if page_type != PageType.PRODUCT:
                    log.info("skip_non_product", url=dp.url, type=page_type.value)
                    checkpoint.mark(dp.url)
                    continue

                product = extractor.extract(
                    dp.url, result.html, dp.category, dp.subcategory
                )
                if product and validator.validate(product):
                    storage.upsert(product)
                checkpoint.mark(dp.url)
            except Exception as e:
                errors += 1
                log.warning("product_failed", url=dp.url, error=str(e))
                # Not checkpointed -> a later run retries it.

        exports = storage.export(cfg.storage.export_dir, cfg.storage.exports)

    summary = {
        "duration_seconds": round(time.time() - started, 1),
        "discovered": len(discovered),
        "stored_total": storage.count(),
        "errors": errors,
        "llm_calls": llm.calls,
        "quality": validator.report.as_dict(),
        "exports": exports,
    }
    Path("data").mkdir(exist_ok=True)
    Path("data/run_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    storage.close()

    typer.echo("\n=== RUN SUMMARY ===")
    typer.echo(json.dumps(summary, indent=2))


@app.command()
def export(config: str = typer.Option("config.yaml")):
    """Re-export the current SQLite store to JSON/CSV without crawling."""
    cfg = Config.load(config)
    storage = Storage(cfg.storage.sqlite_path)
    paths = storage.export(cfg.storage.export_dir, cfg.storage.exports)
    storage.close()
    typer.echo(f"Exported: {paths}")


if __name__ == "__main__":
    app()
